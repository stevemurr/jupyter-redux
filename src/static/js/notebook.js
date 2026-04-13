/**
 * Notebook editor controller.
 * Manages cells, keyboard shortcuts, WebSocket communication,
 * and the command/edit mode state machine.
 */

class NotebookEditor {
    constructor() {
        this.notebookId = getNotebookId();
        this.environmentId = getEnvironmentId();
        this.notebook = null;
        this.cells = new Map(); // cellId -> CellComponent
        this.cellOrder = [];    // ordered cell IDs
        this.selectedCellId = null;
        this.mode = 'command';  // 'command' or 'edit'
        this.ws = null;
        this.executingCellId = null;
        this.deleteBuffer = [];  // for undo delete (Z)
        this.lastDKey = 0;       // for DD shortcut

        // Auto-save
        this._saveTimer = null;
        this._saveDelay = 500;

        // DOM references
        this.cellsContainer = document.getElementById('cells-container');
        this.titleInput = document.getElementById('notebook-title');
        this.saveIndicator = document.getElementById('save-indicator');
        this.containerBadge = document.getElementById('container-badge');
        this.modeIndicator = document.getElementById('mode-indicator');
        this.btnInterrupt = document.getElementById('btn-interrupt');
        this.buildLogEl = document.getElementById('build-log');
        this.buildLogContent = document.getElementById('build-log-content');

        this._bindToolbar();
        this._bindKeyboard();
        this._waitForCodeMirror();
    }

    _waitForCodeMirror() {
        if (window.EditorView) {
            this._init();
        } else {
            window.addEventListener('codemirror-ready', () => this._init(), { once: true });
        }
    }

    async _init() {
        if (!this.notebookId) {
            this.cellsContainer.innerHTML = '<p class="empty-state">No notebook ID found in URL.</p>';
            return;
        }

        try {
            await this._loadAndRender();
        } catch (e) {
            console.error('Notebook init failed:', e);
        }

        // Connect WebSocket regardless of render errors
        this._connectWebSocket();

        // Title change handler
        this.titleInput.addEventListener('change', () => this._renameNotebook());
        this.titleInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this.titleInput.blur();
            }
        });

        window.addEventListener('beforeunload', () => this._saveAllCells());
    }

    async _loadAndRender() {
        const data = await api.get(`/api/notebooks/${this.notebookId}`);
        if (!data) {
            this.cellsContainer.innerHTML = '<p class="empty-state">Failed to load notebook.</p>';
            return;
        }

        this.notebook = data;
        this.environmentId = data.environment_id || this.environmentId;
        this.titleInput.value = data.name;
        document.title = `${data.name} - Jupyter Redux`;

        // Set back link to environment page
        const backLink = document.getElementById('back-link');
        if (backLink && this.environmentId) {
            backLink.href = `/environment/${this.environmentId}`;
        }

        for (const cellData of data.cells) {
            this._addCellComponent(cellData);
        }

        if (this.cellOrder.length > 0) {
            this.selectCell(this.cellOrder[0]);
        }

        if (data.container_state) {
            this._updateContainerBadge(data.container_state.status);
        }
    }

    _connectWebSocket() {
        console.log('[NB] _connectWebSocket called for', this.notebookId);
        this.ws = new NotebookWebSocket(this.notebookId);

        this.ws.on('container_state', (msg) => {
            this._updateContainerBadge(msg.status, msg.message);
            if (msg.status === 'building') {
                this._showBuildLog(true);
            } else if (msg.status === 'ready') {
                this._showBuildLog(false);
            }
        });

        this.ws.on('build_log', (msg) => {
            this._appendBuildLog(msg.lines || []);
        });

        this.ws.on('state', (msg) => {
            const cell = this.cells.get(msg.cell_id);
            if (cell) {
                if (msg.execution_state === 'running') {
                    // Fresh execution (or replay): clear stale outputs so
                    // the message stream renders cleanly.
                    cell.clearOutputs();
                }
                cell.setExecutionState(msg.execution_state, msg.execution_count);
                if (msg.execution_state === 'running') {
                    this.executingCellId = msg.cell_id;
                    this.btnInterrupt.style.display = 'inline-flex';
                } else if (msg.execution_state === 'completed' || msg.execution_state === 'errored') {
                    if (this.executingCellId === msg.cell_id) {
                        this.executingCellId = null;
                        this.btnInterrupt.style.display = 'none';
                    }
                }
            }
        });

        this.ws.on('output', (msg) => {
            const cell = this.cells.get(msg.cell_id);
            if (cell) {
                cell.appendOutput(msg.stream, msg.text);
            }
        });

        this.ws.on('display', (msg) => {
            const cell = this.cells.get(msg.cell_id);
            if (cell) {
                cell.appendDisplay(msg.display_type, msg.data, msg.mime, msg.filename);
            }
        });

        this.ws.on('result', (msg) => {
            const cell = this.cells.get(msg.cell_id);
            if (cell) {
                cell.appendOutput('result', msg.data);
            }
        });

        this.ws.on('error', (msg) => {
            const cell = this.cells.get(msg.cell_id);
            if (cell) {
                cell.appendOutput('error', {
                    ename: msg.ename,
                    evalue: msg.evalue,
                    traceback: msg.traceback,
                });
            }
        });

        this.ws.on('send_failed', (msg) => {
            if (msg.type === 'execute') {
                const cell = this.cells.get(msg.cell_id);
                if (cell) {
                    cell.setExecutionState('errored');
                    cell.appendOutput('error', {
                        ename: 'ConnectionError',
                        evalue: 'Not connected. Retrying...',
                        traceback: [],
                    });
                }
            }
        });

        this.ws.on('reconnecting', (data) => {
            this._updateContainerBadge('starting', `Reconnecting (attempt ${data.attempt})...`);
        });

        this.ws.on('reconnect_failed', () => {
            this._updateContainerBadge('error', 'Connection lost. Refresh the page.');
        });

        this.ws.connect();
    }

    _updateContainerBadge(status, message) {
        const badge = this.containerBadge;
        const dot = badge.querySelector('.badge-dot');
        const text = badge.querySelector('.badge-text');

        badge.className = `container-badge ${status}`;
        const labels = {
            none: 'No container',
            building: message || 'Building image...',
            starting: 'Starting...',
            ready: 'Connected',
            stopping: 'Stopping...',
            stopped: 'Stopped',
            error: message || 'Error',
        };
        text.textContent = labels[status] || status;

        const notReady = status !== 'ready';
        document.getElementById('btn-add-code').disabled = notReady;
        document.getElementById('btn-add-markdown').disabled = notReady;
        document.getElementById('btn-run-all').disabled = notReady;
    }

    _showBuildLog(show) {
        if (this.buildLogEl) {
            this.buildLogEl.style.display = show ? 'block' : 'none';
            if (show && this.buildLogContent) {
                this.buildLogContent.textContent = '';
            }
        }
    }

    _appendBuildLog(lines) {
        if (!this.buildLogContent) return;
        for (const line of lines) {
            this.buildLogContent.textContent += line + '\n';
        }
        // Auto-scroll to bottom
        this.buildLogContent.scrollTop = this.buildLogContent.scrollHeight;
    }

    // --- Cell Management ---

    _addCellComponent(cellData, position) {
        const cell = new CellComponent(cellData, this);

        if (position != null && position < this.cellOrder.length) {
            const refId = this.cellOrder[position];
            const refEl = this.cells.get(refId)?.element;
            this.cellsContainer.insertBefore(cell.element, refEl);
            this.cellOrder.splice(position, 0, cell.id);
        } else {
            this.cellsContainer.appendChild(cell.element);
            this.cellOrder.push(cell.id);
        }

        this.cells.set(cell.id, cell);
        return cell;
    }

    selectCell(cellId) {
        if (this.selectedCellId === cellId) return;

        // Deselect current
        if (this.selectedCellId) {
            const prev = this.cells.get(this.selectedCellId);
            if (prev) {
                prev.setSelected(false);
                prev.setEditMode(false);
            }
        }

        this.selectedCellId = cellId;
        const cell = this.cells.get(cellId);
        if (cell) {
            cell.setSelected(true);
            cell.element.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }

        // Reset to command mode on cell switch
        this.enterCommandMode();
    }

    enterCommandMode() {
        this.mode = 'command';
        this.modeIndicator.className = 'mode-indicator command';
        this.modeIndicator.textContent = 'COMMAND';

        const cell = this.cells.get(this.selectedCellId);
        if (cell) {
            cell.setEditMode(false);
        }
    }

    enterEditMode() {
        this.mode = 'edit';
        this.modeIndicator.className = 'mode-indicator edit';
        this.modeIndicator.textContent = 'EDIT';

        const cell = this.cells.get(this.selectedCellId);
        if (cell) {
            cell.setSelected(true);
            cell.setEditMode(true);
        }
    }

    _getSelectedIndex() {
        return this.cellOrder.indexOf(this.selectedCellId);
    }

    selectPrevious() {
        const idx = this._getSelectedIndex();
        if (idx > 0) {
            this.selectCell(this.cellOrder[idx - 1]);
        }
    }

    selectNext() {
        const idx = this._getSelectedIndex();
        if (idx < this.cellOrder.length - 1) {
            this.selectCell(this.cellOrder[idx + 1]);
        }
    }

    async insertCellAbove(type = 'code') {
        const idx = this._getSelectedIndex();
        const position = Math.max(0, idx);
        const data = await api.post(`/api/notebooks/${this.notebookId}/cells`, {
            cell_type: type,
            source: '',
            position,
        });
        if (data && data.cells) {
            // Find the new cell (at position)
            const newCellData = data.cells[position];
            if (newCellData) {
                this._addCellComponent(newCellData, position);
                this.selectCell(newCellData.id);
                this.enterEditMode();
            }
        }
    }

    async insertCellBelow(type = 'code') {
        const idx = this._getSelectedIndex();
        const position = idx + 1;
        const data = await api.post(`/api/notebooks/${this.notebookId}/cells`, {
            cell_type: type,
            source: '',
            position,
        });
        if (data && data.cells) {
            const newCellData = data.cells[position];
            if (newCellData) {
                this._addCellComponent(newCellData, position);
                this.selectCell(newCellData.id);
                this.enterEditMode();
            }
        }
    }

    async deleteCell(cellId) {
        const cell = this.cells.get(cellId);
        if (!cell) return;

        // Buffer for undo
        this.deleteBuffer.push({
            id: cell.id,
            cellType: cell.cellType,
            source: cell.getSource(),
            position: this.cellOrder.indexOf(cellId),
        });

        const idx = this.cellOrder.indexOf(cellId);
        const data = await api.del(`/api/notebooks/${this.notebookId}/cells/${cellId}`);
        if (data !== false) {
            cell.destroy();
            this.cells.delete(cellId);
            this.cellOrder.splice(idx, 1);

            // Select adjacent cell
            if (this.cellOrder.length > 0) {
                const newIdx = Math.min(idx, this.cellOrder.length - 1);
                this.selectCell(this.cellOrder[newIdx]);
            } else {
                this.selectedCellId = null;
            }
        }
    }

    async undoDelete() {
        if (this.deleteBuffer.length === 0) return;
        const entry = this.deleteBuffer.pop();

        const data = await api.post(`/api/notebooks/${this.notebookId}/cells`, {
            cell_type: entry.cellType,
            source: entry.source,
            position: entry.position,
        });
        if (data && data.cells) {
            const newCellData = data.cells[entry.position];
            if (newCellData) {
                this._addCellComponent(newCellData, entry.position);
                this.selectCell(newCellData.id);
            }
        }
    }

    changeCellType(type) {
        const cell = this.cells.get(this.selectedCellId);
        if (!cell || cell.cellType === type) return;

        cell.setCellType(type);
        // Persist
        api.put(`/api/notebooks/${this.notebookId}/cells/${cell.id}`, { cell_type: type });
    }

    // --- Execution ---

    executeCell(cellId) {
        const cell = this.cells.get(cellId);
        if (!cell || cell.cellType !== 'code') return;

        cell.clearOutputs();
        cell.setExecutionState('running');
        const source = cell.getSource();
        this.ws.execute(cellId, source);
    }

    executeCellAndAdvance(cellId) {
        this.executeCell(cellId);
        const idx = this.cellOrder.indexOf(cellId);
        if (idx < this.cellOrder.length - 1) {
            this.selectCell(this.cellOrder[idx + 1]);
        } else {
            // Insert new cell below if at end
            this.insertCellBelow();
        }
    }

    executeCellAndInsertBelow(cellId) {
        this.executeCell(cellId);
        this.insertCellBelow();
    }

    interruptCell(cellId) {
        const target = cellId || this.executingCellId;
        if (target && this.ws) {
            this.ws.interrupt(target);
        }
    }

    async runAllCells() {
        for (const cellId of this.cellOrder) {
            const cell = this.cells.get(cellId);
            if (cell && cell.cellType === 'code') {
                cell.clearOutputs();
                cell.setExecutionState('running');
                this.ws.execute(cellId, cell.getSource());
                // Wait until this cell finishes
                await new Promise((resolve) => {
                    const handler = (msg) => {
                        if (msg.cell_id === cellId &&
                            (msg.execution_state === 'completed' || msg.execution_state === 'errored')) {
                            this.ws.off('state', handler);
                            resolve();
                        }
                    };
                    this.ws.on('state', handler);
                });
            }
        }
    }

    // --- Auto-save ---

    onCellChanged(cellId, source) {
        clearTimeout(this._saveTimer);
        this._showSaveStatus('saving');
        this._saveTimer = setTimeout(() => this._saveCell(cellId, source), this._saveDelay);
    }

    async _saveCell(cellId, source) {
        const result = await api.put(
            `/api/notebooks/${this.notebookId}/cells/${cellId}`,
            { source }
        );
        if (result) {
            this._showSaveStatus('saved');
        } else {
            this._showSaveStatus('error');
        }
    }

    _saveAllCells() {
        clearTimeout(this._saveTimer);
        for (const [cellId, cell] of this.cells) {
            const source = cell.getSource();
            if (source !== cell.source) {
                // Fire-and-forget on unload
                navigator.sendBeacon && false; // Beacon doesn't support JSON easily
                api.put(`/api/notebooks/${this.notebookId}/cells/${cellId}`, { source });
            }
        }
    }

    _showSaveStatus(status) {
        this.saveIndicator.className = `save-indicator ${status}`;
        const labels = { saving: 'Saving...', saved: 'Saved', error: 'Save failed' };
        this.saveIndicator.textContent = labels[status] || '';

        if (status === 'saved') {
            setTimeout(() => {
                if (this.saveIndicator.textContent === 'Saved') {
                    this.saveIndicator.textContent = '';
                }
            }, 2000);
        }
    }

    async _renameNotebook() {
        const newName = this.titleInput.value.trim();
        if (!newName || newName === this.notebook.name) return;

        const data = await api.put(`/api/notebooks/${this.notebookId}`, { name: newName });
        if (data) {
            this.notebook.name = newName;
            document.title = `${newName} - Jupyter Redux`;
        } else {
            this.titleInput.value = this.notebook.name;
        }
    }

    // --- Toolbar ---

    _bindToolbar() {
        document.getElementById('btn-add-code').addEventListener('click', () => this.insertCellBelow('code'));
        document.getElementById('btn-add-markdown').addEventListener('click', () => this.insertCellBelow('markdown'));
        document.getElementById('btn-run-all').addEventListener('click', () => this.runAllCells());
        this.btnInterrupt.addEventListener('click', () => this.interruptCell());
    }

    // --- Keyboard Shortcuts ---

    _bindKeyboard() {
        document.addEventListener('keydown', (e) => {
            // Ignore if typing in title input
            if (e.target === this.titleInput) return;

            if (this.mode === 'command') {
                this._handleCommandKey(e);
            } else {
                this._handleEditKey(e);
            }
        });
    }

    _handleCommandKey(e) {
        // Shift+Enter: execute + advance
        if (e.key === 'Enter' && e.shiftKey) {
            e.preventDefault();
            if (this.selectedCellId) this.executeCellAndAdvance(this.selectedCellId);
            return;
        }

        // Ctrl+Enter: execute in place
        if (e.key === 'Enter' && e.ctrlKey) {
            e.preventDefault();
            if (this.selectedCellId) this.executeCell(this.selectedCellId);
            return;
        }

        // Alt+Enter: execute + insert below
        if (e.key === 'Enter' && e.altKey) {
            e.preventDefault();
            if (this.selectedCellId) this.executeCellAndInsertBelow(this.selectedCellId);
            return;
        }

        // Enter: edit mode
        if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.altKey) {
            e.preventDefault();
            this.enterEditMode();
            return;
        }

        // Escape: stay in command mode (no-op)
        if (e.key === 'Escape') return;

        // Navigation
        if (e.key === 'ArrowUp' || e.key === 'k') {
            e.preventDefault();
            this.selectPrevious();
            return;
        }
        if (e.key === 'ArrowDown' || e.key === 'j') {
            e.preventDefault();
            this.selectNext();
            return;
        }

        // Cell operations
        if (e.key === 'a') {
            e.preventDefault();
            this.insertCellAbove();
            return;
        }
        if (e.key === 'b') {
            e.preventDefault();
            this.insertCellBelow();
            return;
        }

        // DD: delete cell (double-tap D)
        if (e.key === 'd') {
            const now = Date.now();
            if (now - this.lastDKey < 500) {
                e.preventDefault();
                if (this.selectedCellId) this.deleteCell(this.selectedCellId);
                this.lastDKey = 0;
            } else {
                this.lastDKey = now;
            }
            return;
        }

        // Y: change to code
        if (e.key === 'y') {
            e.preventDefault();
            this.changeCellType('code');
            return;
        }

        // M: change to markdown
        if (e.key === 'm') {
            e.preventDefault();
            this.changeCellType('markdown');
            return;
        }

        // Z: undo delete
        if (e.key === 'z' && !e.ctrlKey) {
            e.preventDefault();
            this.undoDelete();
            return;
        }

        // Ctrl+C: interrupt
        if (e.key === 'c' && e.ctrlKey) {
            e.preventDefault();
            this.interruptCell();
            return;
        }
    }

    _handleEditKey(e) {
        // Escape: command mode
        if (e.key === 'Escape') {
            e.preventDefault();
            this.enterCommandMode();
            return;
        }

        // Shift+Enter, Ctrl+Enter, Alt+Enter are handled by CodeMirror keybindings in cell.js
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.notebookEditor = new NotebookEditor();
});
