/**
 * Shared API helpers and utility functions.
 */
const api = {
    async get(url) {
        try {
            const res = await fetch(url);
            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: res.statusText }));
                console.error(`GET ${url}:`, err.detail);
                return null;
            }
            return res.json();
        } catch (e) {
            console.error(`GET ${url}:`, e.message);
            return null;
        }
    },

    async post(url, body = {}) {
        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: res.statusText }));
                console.error(`POST ${url}:`, err.detail);
                return null;
            }
            if (res.status === 204) return {};
            return res.json();
        } catch (e) {
            console.error(`POST ${url}:`, e.message);
            return null;
        }
    },

    async put(url, body = {}) {
        try {
            const res = await fetch(url, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: res.statusText }));
                console.error(`PUT ${url}:`, err.detail);
                return null;
            }
            return res.json();
        } catch (e) {
            console.error(`PUT ${url}:`, e.message);
            return null;
        }
    },

    async del(url) {
        try {
            const res = await fetch(url, { method: 'DELETE' });
            if (!res.ok && res.status !== 204) {
                const err = await res.json().catch(() => ({ detail: res.statusText }));
                console.error(`DELETE ${url}:`, err.detail);
                return false;
            }
            return true;
        } catch (e) {
            console.error(`DELETE ${url}:`, e.message);
            return false;
        }
    },
};

function getNotebookId() {
    const parts = window.location.pathname.split('/');
    const idx = parts.indexOf('notebook');
    return idx >= 0 ? parts[idx + 1] : null;
}

/**
 * Maps backend error details to actionable user-facing messages.
 */
function getActionableError(detail) {
    if (!detail) return 'An unexpected error occurred.';
    const s = String(detail).toLowerCase();
    if (s.includes('docker is not running') || s.includes('connectionrefused'))
        return 'Docker is not running. Start Docker and try again.';
    if (s.includes('not enough memory') || s.includes('oom'))
        return 'Not enough memory to start notebook. Close other notebooks and try again.';
    if (s.includes('gpu') || s.includes('nvidia'))
        return 'GPU support requires NVIDIA Container Toolkit. Notebook will run without GPU.';
    if (s.includes('disk') || s.includes('no space'))
        return 'Disk space exhausted. Free up disk space and try again.';
    if (s.includes('not found'))
        return 'The requested resource was not found.';
    return detail;
}
