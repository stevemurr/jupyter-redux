/**
 * Cell component for the notebook editor.
 * Creates cell DOM elements with CodeMirror 6 editors,
 * output display, execution state management, and cell toolbars.
 */

/* global EditorView, EditorState, python, keymap, defaultKeymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, syntaxHighlighting, defaultHighlightStyle, oneDark */

class CellComponent {
    constructor(cellData, notebook) {
        this.id = cellData.id;
        this.cellType = cellData.cell_type || 'code';
        this.source = cellData.source || '';
        this.outputs = cellData.outputs || [];
        this.executionCount = cellData.execution_count;
        this.executionState = cellData.execution_state || 'idle';
        this.notebook = notebook;
        this.editor = null;
        this.element = null;
        this.isEditMode = false;
        this._buildElement();
    }

    _buildElement() {
        this.element = document.createElement('div');
        this.element.className = 'cell';
        this.element.dataset.cellId = this.id;
        this.element.setAttribute('role', 'listitem');
        this.element.setAttribute('tabindex', '0');

        this._updateCellClasses();

        // Header
        this.headerEl = document.createElement('div');
        this.headerEl.className = 'cell-header';

        this.execCountEl = document.createElement('span');
        this.execCountEl.className = 'cell-exec-count';
        this._updateExecCount();

        this.stateIndicator = document.createElement('span');
        this.stateIndicator.className = 'exec-indicator';
        this._updateStateIndicator();

        this.typeBadge = document.createElement('span');
        this.typeBadge.className = 'cell-type-badge';
        this.typeBadge.textContent = this.cellType;

        this.toolbar = document.createElement('div');
        this.toolbar.className = 'cell-toolbar';
        this._buildToolbar();

        this.headerEl.appendChild(this.execCountEl);
        this.headerEl.appendChild(this.stateIndicator);
        this.headerEl.appendChild(this.typeBadge);
        this.headerEl.appendChild(this.toolbar);

        // Editor
        this.editorContainer = document.createElement('div');
        this.editorContainer.className = 'cell-editor';

        // Markdown rendered view
        this.markdownRendered = document.createElement('div');
        this.markdownRendered.className = 'markdown-rendered';
        this.markdownRendered.style.display = 'none';

        // Output wrapper (collapsible)
        this.outputWrapper = document.createElement('div');
        this.outputWrapper.className = 'cell-output-wrapper collapsed';

        this.outputToggle = document.createElement('button');
        this.outputToggle.className = 'output-toggle';
        this.outputToggle.setAttribute('aria-label', 'Toggle output');
        this.outputToggle.addEventListener('click', (e) => {
            e.stopPropagation();
            this._toggleOutput();
        });

        this.outputEl = document.createElement('div');
        this.outputEl.className = 'cell-output';
        this.outputEl.setAttribute('role', 'log');
        this.outputEl.setAttribute('aria-label', 'Cell output');

        this.outputWrapper.appendChild(this.outputToggle);
        this.outputWrapper.appendChild(this.outputEl);
        this._renderOutputs();

        this.element.appendChild(this.headerEl);
        if (this.cellType === 'markdown') {
            this.element.appendChild(this.markdownRendered);
        }
        this.element.appendChild(this.editorContainer);
        this.element.appendChild(this.outputWrapper);

        // Click handler for selection
        this.element.addEventListener('click', (e) => {
            if (!e.target.closest('.cell-toolbar') && !e.target.closest('button')) {
                this.notebook.selectCell(this.id);
            }
        });

        // Initialize editor after element is in DOM (deferred)
        requestAnimationFrame(() => this._initEditor());

        if (this.cellType === 'markdown' && this.source) {
            this._renderMarkdown();
        }
    }

    _buildToolbar() {
        const runBtn = document.createElement('button');
        runBtn.className = 'btn-icon';
        runBtn.innerHTML = '&#9654;';
        runBtn.title = 'Run cell (Shift+Enter)';
        runBtn.setAttribute('aria-label', 'Run cell');
        runBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.notebook.executeCell(this.id);
        });

        this.stopBtn = document.createElement('button');
        this.stopBtn.className = 'btn-icon';
        this.stopBtn.innerHTML = '&#9632;';
        this.stopBtn.title = 'Interrupt execution';
        this.stopBtn.setAttribute('aria-label', 'Stop execution');
        this.stopBtn.style.display = 'none';
        this.stopBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.notebook.interruptCell(this.id);
        });

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'btn-icon';
        deleteBtn.innerHTML = '&#10005;';
        deleteBtn.title = 'Delete cell (DD)';
        deleteBtn.setAttribute('aria-label', 'Delete cell');
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.notebook.deleteCell(this.id);
        });

        this.toolbar.appendChild(runBtn);
        this.toolbar.appendChild(this.stopBtn);
        this.toolbar.appendChild(deleteBtn);
    }

    _initEditor() {
        if (this.editor) return;

        const extensions = [
            lineNumbers(),
            highlightActiveLine(),
            highlightActiveLineGutter(),
            EditorView.lineWrapping,
            syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
            oneDark,
            EditorView.updateListener.of((update) => {
                if (update.docChanged) {
                    this.source = update.state.doc.toString();
                    this._updateCellClasses();
                    if (this.notebook) {
                        this.notebook.onCellChanged(this.id, this.source);
                    }
                }
            }),
            EditorView.domEventHandlers({
                focus: () => {
                    this.notebook.selectCell(this.id);
                    this.notebook.enterEditMode();
                },
            }),
            keymap.of([
                {
                    key: 'Shift-Enter',
                    run: () => {
                        this.notebook.executeCellAndAdvance(this.id);
                        return true;
                    },
                },
                {
                    key: 'Ctrl-Enter',
                    run: () => {
                        this.notebook.executeCell(this.id);
                        return true;
                    },
                },
                {
                    key: 'Alt-Enter',
                    run: () => {
                        this.notebook.executeCellAndInsertBelow(this.id);
                        return true;
                    },
                },
                {
                    key: 'Escape',
                    run: () => {
                        this.notebook.enterCommandMode();
                        this.editor.contentDOM.blur();
                        return true;
                    },
                },
                ...defaultKeymap,
            ]),
            ...(this.cellType === 'code' ? [python()] : []),
        ];

        this.editor = new EditorView({
            state: EditorState.create({
                doc: this.source,
                extensions,
            }),
            parent: this.editorContainer,
        });

        // Hide editor for rendered markdown cells
        if (this.cellType === 'markdown' && !this.isEditMode) {
            this.editorContainer.style.display = 'none';
            this.markdownRendered.style.display = 'block';
        }
    }

    _updateCellClasses() {
        const classes = ['cell'];
        if (this.cellType === 'markdown') classes.push('markdown-cell');
        if (this._isPipInstall()) classes.push('setup-cell');
        if (this.executionState && this.executionState !== 'idle') {
            classes.push(this.executionState);
        }
        if (this.element) {
            // Preserve selected/edit-mode classes
            if (this.element.classList.contains('selected')) classes.push('selected');
            if (this.element.classList.contains('edit-mode')) classes.push('edit-mode');
            this.element.className = classes.join(' ');
        }
    }

    _isPipInstall() {
        const s = this.source.trim();
        if (s.startsWith('pip install') || s.startsWith('pip uninstall')) return true;
        // Detect requirements.txt style: 2+ lines that all look like package specifiers
        const lines = s.split('\n').filter(l => l.trim() && !l.trim().startsWith('#'));
        if (lines.length < 2) return false;
        const reqRe = /^[a-zA-Z0-9][\w.*-]*(\[[\w,.-]+\])?([<>=!~]+[\w.*]+)?(,\s*[<>=!~]+[\w.*]+)*$/;
        return lines.every(l => reqRe.test(l.trim()));
    }

    _updateExecCount() {
        if (this.executionState === 'running') {
            this.execCountEl.textContent = 'In [*]:';
            this.execCountEl.className = 'cell-exec-count running';
        } else if (this.executionCount != null) {
            this.execCountEl.textContent = `In [${this.executionCount}]:`;
            this.execCountEl.className = 'cell-exec-count';
        } else {
            this.execCountEl.textContent = 'In [ ]:';
            this.execCountEl.className = 'cell-exec-count';
        }
    }

    _updateStateIndicator() {
        if (this.executionState === 'running') {
            this.stateIndicator.className = 'exec-indicator running';
            this.stateIndicator.innerHTML = '&#10227;'; // Circular arrow
            this.stateIndicator.setAttribute('aria-label', 'Running');
        } else if (this.executionState === 'completed') {
            this.stateIndicator.className = 'exec-indicator completed';
            this.stateIndicator.innerHTML = '&#10003;';
            this.stateIndicator.setAttribute('aria-label', 'Completed');
        } else if (this.executionState === 'errored') {
            this.stateIndicator.className = 'exec-indicator errored';
            this.stateIndicator.innerHTML = '&#10007;';
            this.stateIndicator.setAttribute('aria-label', 'Error');
        } else {
            this.stateIndicator.className = 'exec-indicator';
            this.stateIndicator.innerHTML = '';
            this.stateIndicator.setAttribute('aria-label', 'Idle');
        }
    }

    setExecutionState(state, executionCount) {
        this.executionState = state;
        if (executionCount != null) {
            this.executionCount = executionCount;
        }
        this._updateExecCount();
        this._updateStateIndicator();
        this._updateCellClasses();

        // Toggle stop button visibility
        if (this.stopBtn) {
            this.stopBtn.style.display = state === 'running' ? 'inline-flex' : 'none';
        }

        // Show install badge for pip cells
        if (this._isPipInstall()) {
            this._updateInstallBadge(state);
        }
    }

    _updateInstallBadge(state) {
        let badge = this.headerEl.querySelector('.install-badge');
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'install-badge';
            this.headerEl.insertBefore(badge, this.toolbar);
        }
        if (state === 'running') {
            badge.className = 'install-badge installing';
            badge.textContent = 'Installing...';
        } else if (state === 'completed') {
            badge.className = 'install-badge success';
            badge.textContent = 'Installed';
        } else if (state === 'errored') {
            badge.className = 'install-badge failure';
            badge.textContent = 'Failed';
        } else {
            badge.remove();
        }
    }

    _toggleOutput() {
        this.outputWrapper.classList.toggle('collapsed');
        this._updateToggleLabel();
    }

    _updateToggleLabel() {
        const collapsed = this.outputWrapper.classList.contains('collapsed');
        const lines = (this.outputEl.textContent || '').split('\n').filter(l => l.trim()).length;
        if (collapsed) {
            this.outputToggle.innerHTML = `<span class="toggle-icon">&#9654;</span> Output <span class="toggle-hint">${lines} line${lines !== 1 ? 's' : ''} — click to expand</span>`;
        } else {
            this.outputToggle.innerHTML = `<span class="toggle-icon">&#9660;</span> Output <span class="toggle-hint">click to collapse</span>`;
        }
    }

    _showOutputWrapper() {
        if (this.outputEl.children.length > 0 || this.outputEl.textContent.trim()) {
            this.outputWrapper.style.display = 'block';
            this._updateToggleLabel();
        }
    }

    clearOutputs() {
        this.outputs = [];
        this.outputEl.innerHTML = '';
        this.outputWrapper.style.display = 'none';
        this.outputWrapper.classList.add('collapsed');
    }

    appendOutput(outputType, content) {
        if (outputType === 'error') {
            const errorDiv = document.createElement('div');
            errorDiv.className = 'output-error';
            if (content.ename) {
                const nameSpan = document.createElement('span');
                nameSpan.className = 'error-name';
                nameSpan.textContent = `${content.ename}: ${content.evalue}`;
                errorDiv.appendChild(nameSpan);
            }
            if (content.traceback && content.traceback.length) {
                const tbPre = document.createElement('pre');
                tbPre.className = 'traceback';
                tbPre.textContent = content.traceback.join('\n');
                errorDiv.appendChild(tbPre);
            }
            this.outputEl.appendChild(errorDiv);
        } else {
            // Reuse existing <pre> for the same stream to avoid excessive spacing
            const lastChild = this.outputEl.lastElementChild;
            if (lastChild && lastChild.tagName === 'PRE' && lastChild.className === `output-${outputType}`) {
                lastChild.textContent += content;
            } else {
                const pre = document.createElement('pre');
                pre.className = `output-${outputType}`;
                pre.textContent = content;
                this.outputEl.appendChild(pre);
            }
        }
        this._showOutputWrapper();
    }

    appendDisplay(displayType, data, mime, filename) {
        if (displayType === 'audio') {
            const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
            const blob = new Blob([bytes], { type: mime });
            const url = URL.createObjectURL(blob);

            const wrapper = document.createElement('div');
            wrapper.className = 'output-display output-audio';

            const label = document.createElement('span');
            label.className = 'audio-filename';
            label.textContent = filename || 'audio';
            wrapper.appendChild(label);

            const audio = document.createElement('audio');
            audio.controls = true;
            audio.preload = 'metadata';
            audio.src = url;
            wrapper.appendChild(audio);

            this.outputEl.appendChild(wrapper);
        }
        this._showOutputWrapper();
    }

    _renderOutputs() {
        this.outputEl.innerHTML = '';
        this.outputWrapper.style.display = 'none';
        for (const output of this.outputs) {
            if (output.output_type === 'error') {
                this.appendOutput('error', { text: output.content });
            } else {
                this.appendOutput(output.output_type, output.content);
            }
        }
    }

    setSelected(selected) {
        this.element.classList.toggle('selected', selected);
    }

    setEditMode(editMode) {
        this.isEditMode = editMode;
        this.element.classList.toggle('edit-mode', editMode);

        if (this.cellType === 'markdown') {
            if (editMode) {
                this.editorContainer.style.display = 'block';
                this.markdownRendered.style.display = 'none';
                if (this.editor) this.editor.focus();
            } else {
                this.editorContainer.style.display = 'none';
                this.markdownRendered.style.display = 'block';
                this._renderMarkdown();
            }
        } else if (editMode && this.editor) {
            this.editor.focus();
        }
    }

    focus() {
        if (this.editor) {
            this.editor.focus();
        }
    }

    getSource() {
        if (this.editor) {
            return this.editor.state.doc.toString();
        }
        return this.source;
    }

    setSource(source) {
        this.source = source;
        if (this.editor) {
            this.editor.dispatch({
                changes: {
                    from: 0,
                    to: this.editor.state.doc.length,
                    insert: source,
                },
            });
        }
    }

    setCellType(type) {
        this.cellType = type;
        this.typeBadge.textContent = type;
        this._updateCellClasses();

        if (type === 'markdown') {
            this.element.insertBefore(this.markdownRendered, this.editorContainer);
            if (!this.isEditMode) {
                this.editorContainer.style.display = 'none';
                this.markdownRendered.style.display = 'block';
                this._renderMarkdown();
            }
            // Clear outputs for markdown cells
            this.clearOutputs();
        } else {
            this.editorContainer.style.display = 'block';
            this.markdownRendered.style.display = 'none';
        }
    }

    _renderMarkdown() {
        this.markdownRendered.innerHTML = this._parseMarkdown(this.source || '');
    }

    _parseMarkdown(text) {
        if (!text.trim()) {
            return '<p style="color: var(--text-muted); font-style: italic;">Empty markdown cell. Press Enter to edit.</p>';
        }

        let html = text
            // Escape HTML
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            // Code blocks
            .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
            // Inline code
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            // Headers
            .replace(/^### (.+)$/gm, '<h3>$1</h3>')
            .replace(/^## (.+)$/gm, '<h2>$1</h2>')
            .replace(/^# (.+)$/gm, '<h1>$1</h1>')
            // Bold and italic
            .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.+?)\*/g, '<em>$1</em>')
            // Links
            .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
            // Blockquotes
            .replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>')
            // Unordered lists
            .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
            // Ordered lists
            .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
            // Paragraphs - wrap remaining loose lines
            .replace(/^(?!<[hpuolb]|<li|<pre|<code)(.+)$/gm, '<p>$1</p>');

        // Wrap consecutive <li> elements in <ul>
        html = html.replace(/(<li>[\s\S]*?<\/li>\n?)+/g, '<ul>$&</ul>');

        return html;
    }

    destroy() {
        if (this.editor) {
            this.editor.destroy();
            this.editor = null;
        }
        if (this.element && this.element.parentNode) {
            this.element.parentNode.removeChild(this.element);
        }
    }
}
