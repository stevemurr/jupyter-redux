/**
 * WebSocket client wrapper for notebook execution.
 * Connects to ws://host/ws/notebooks/{id}, parses JSON messages,
 * dispatches callbacks by type, handles auto-reconnect.
 * Queues messages while disconnected and flushes on reconnect.
 */
class NotebookWebSocket {
    constructor(notebookId) {
        this.notebookId = notebookId;
        this.ws = null;
        this.callbacks = {};
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 10;
        this.baseDelay = 1000;
        this.maxDelay = 30000;
        this.intentionallyClosed = false;
        this._pendingQueue = [];
    }

    on(type, callback) {
        if (!this.callbacks[type]) {
            this.callbacks[type] = [];
        }
        this.callbacks[type].push(callback);
        return this;
    }

    off(type, callback) {
        if (this.callbacks[type]) {
            this.callbacks[type] = this.callbacks[type].filter(cb => cb !== callback);
        }
        return this;
    }

    _emit(type, data) {
        const handlers = this.callbacks[type] || [];
        handlers.forEach(cb => {
            try {
                cb(data);
            } catch (e) {
                console.error(`WS handler error for "${type}":`, e);
            }
        });
    }

    connect() {
        if (this.ws && (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)) {
            return;
        }

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${window.location.host}/ws/notebooks/${this.notebookId}`;

        this.intentionallyClosed = false;
        console.log('[WS] connecting to', url);
        this.ws = new WebSocket(url);

        this.ws.onopen = () => {
            console.log('[WS] connected');
            this.reconnectAttempts = 0;
            this._emit('open', {});
            this._flushQueue();
        };

        this.ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                this._emit(msg.type, msg);
                this._emit('message', msg);
            } catch (e) {
                console.error('WS parse error:', e);
            }
        };

        this.ws.onclose = (event) => {
            console.log('[WS] closed, code:', event.code, event.reason);
            this._emit('close', { code: event.code, reason: event.reason });
            if (!this.intentionallyClosed && event.code !== 4004) {
                this._scheduleReconnect();
            }
        };

        this.ws.onerror = (event) => {
            this._emit('error', event);
        };
    }

    _scheduleReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            this._drainQueue();
            this._emit('reconnect_failed', {});
            return;
        }

        const delay = Math.min(
            this.baseDelay * Math.pow(2, this.reconnectAttempts),
            this.maxDelay
        );
        this.reconnectAttempts++;

        this._emit('reconnecting', { attempt: this.reconnectAttempts, delay });

        setTimeout(() => {
            if (!this.intentionallyClosed) {
                this.connect();
            }
        }, delay);
    }

    _flushQueue() {
        while (this._pendingQueue.length > 0) {
            const msg = this._pendingQueue.shift();
            this.ws.send(JSON.stringify(msg));
        }
    }

    _drainQueue() {
        const dropped = this._pendingQueue.splice(0);
        for (const msg of dropped) {
            if (msg.type === 'execute') {
                this._emit('send_failed', msg);
            }
        }
    }

    send(msg) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(msg));
        } else {
            this._pendingQueue.push(msg);
            if (!this.ws || this.ws.readyState === WebSocket.CLOSED) {
                this.connect();
            }
        }
    }

    execute(cellId, code) {
        this.send({ type: 'execute', cell_id: cellId, code });
    }

    interrupt(cellId) {
        this.send({ type: 'interrupt', cell_id: cellId });
    }

    forceStop(cellId) {
        this.send({ type: 'force_stop', cell_id: cellId });
    }

    disconnect() {
        this.intentionallyClosed = true;
        this._drainQueue();
        if (this.ws) {
            this.ws.close(1000, 'Client disconnect');
            this.ws = null;
        }
    }

    get isConnected() {
        return this.ws && this.ws.readyState === WebSocket.OPEN;
    }
}
