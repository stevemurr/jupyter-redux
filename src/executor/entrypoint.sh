#!/usr/bin/env bash
# Executor entrypoint.
#
# Runs as root for a moment to create a user matching the host uid/gid,
# chown the paths that must be writable, bootstrap the env's uv project
# (pyproject.toml + .venv) if this is the first start, then `exec gosu`
# into the executor server running as the venv's Python. Files written
# on host bind mounts stay owned by the host user so the user can touch
# them from a normal shell without sudo.
#
# If JREDUX_HOST_UID is unset or 0 we skip the whole dance and run
# as root with system Python — handy for quick local experiments.

set -e

HOST_UID="${JREDUX_HOST_UID:-0}"
HOST_GID="${JREDUX_HOST_GID:-0}"

VENV_DIR="/env/.venv"
PROJECT_DIR="/env/files"
OLD_LIB_DIR="/env/lib"

# ---------------------------------------------------------------------
#  Root prelude: create user, chown paths
# ---------------------------------------------------------------------

if [[ "$HOST_UID" == "0" ]]; then
    mkdir -p "$PROJECT_DIR"
    exec python /opt/executor/server.py
fi

if ! getent group "$HOST_GID" >/dev/null; then
    groupadd -g "$HOST_GID" jredux
fi
if ! getent passwd "$HOST_UID" >/dev/null; then
    useradd -u "$HOST_UID" -g "$HOST_GID" -M -s /bin/bash -d /home/jredux jredux
fi

mkdir -p \
    /home/jredux/.cache/huggingface \
    /home/jredux/.cache/pip \
    /home/jredux/.cache/torch \
    /home/jredux/.cache/uv \
    "$PROJECT_DIR"

chown -R "$HOST_UID:$HOST_GID" /home/jredux
chown -R "$HOST_UID:$HOST_GID" /env 2>/dev/null || true

export HOME=/home/jredux
# Tell uv to use /env/.venv as the project venv instead of its
# default of <project>/.venv (which would put the venv inside
# /env/files alongside user notebooks). Must be set everywhere
# uv is invoked — bootstrap, cell execution, the Sync button.
export UV_PROJECT_ENVIRONMENT=/env/.venv
# The uv cache volume is on a different docker filesystem than /env,
# so hardlinks can't span them. Copy mode is slightly slower per
# install but silences the warning and works reliably.
export UV_LINK_MODE=copy

# ---------------------------------------------------------------------
#  uv project bootstrap (as jredux via gosu)
# ---------------------------------------------------------------------
#
# We run the bootstrap through `gosu bash <<'BOOTSTRAP'` with a quoted
# heredoc so variable expansion happens INSIDE the child shell, not in
# the parent. Previously we tried `declare -f` + `bash -c "..."` and
# variables ended up empty in the child. That failure mode was silent
# because stderr was redirected to a log file — so the venv never got
# created and the executor silently ran under system Python.
#
# Steps:
#   1. Write a minimal pyproject.toml if missing.
#   2. Create /env/.venv if missing via `uv venv`.
#   3. Write a .pth file into the venv's site-packages pointing at
#      /env/files so `from utils import foo` still works with files
#      dropped in the workspace — preserves the old PYTHONPATH magic.
#   4. If a uv.lock exists, run `uv sync --frozen` to reconcile the
#      venv. Non-fatal if it fails.

if ! gosu "$HOST_UID:$HOST_GID" bash 2>/tmp/jredux-bootstrap.log <<'BOOTSTRAP'
set -e
export HOME=/home/jredux
# Tell uv to use /env/.venv as the project venv instead of its
# default of <project>/.venv (which would put the venv inside
# /env/files alongside user notebooks). Must be set everywhere
# uv is invoked — bootstrap, cell execution, the Sync button.
export UV_PROJECT_ENVIRONMENT=/env/.venv
# The uv cache volume is on a different docker filesystem than /env,
# so hardlinks can't span them. Copy mode is slightly slower per
# install but silences the warning and works reliably.
export UV_LINK_MODE=copy

VENV_DIR=/env/.venv
PROJECT_DIR=/env/files
PYPROJECT=/env/files/pyproject.toml

if [ ! -f "$PYPROJECT" ]; then
    cat >"$PYPROJECT" <<'PYPROJ'
[project]
name = "jupyter-redux-env"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []
PYPROJ
fi

if [ ! -d "$VENV_DIR" ]; then
    uv venv "$VENV_DIR"
fi

# Preserve workspace-as-PYTHONPATH behavior via a .pth file inside
# the venv's site-packages.
site_pkgs=$("$VENV_DIR/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
if [ -n "$site_pkgs" ] && [ -d "$site_pkgs" ]; then
    printf '%s\n' "$PROJECT_DIR" >"$site_pkgs/jredux-workspace.pth"
fi

if [ -f "$PROJECT_DIR/uv.lock" ]; then
    (cd "$PROJECT_DIR" && uv sync --frozen) || true
fi
BOOTSTRAP
then
    echo "[entrypoint] venv bootstrap failed, falling back to system python." >&2
    echo "[entrypoint] bootstrap log:" >&2
    cat /tmp/jredux-bootstrap.log >&2 || true
    exec gosu "$HOST_UID:$HOST_GID" python /opt/executor/server.py
fi

# As root: clean up the old flat install target from the pre-uv model.
# Safe because .venv is now established. Frees disk space on any env
# that was migrating from the /env/lib flow.
if [ -d "$OLD_LIB_DIR" ]; then
    rm -rf "$OLD_LIB_DIR"
fi

# ---------------------------------------------------------------------
#  Exec the server with the venv's Python
# ---------------------------------------------------------------------

if [ -x "$VENV_DIR/bin/python" ]; then
    # Activating the venv via PATH lets CLI tools (pytest, black, etc.)
    # resolve to the venv binary. The explicit python path in argv is
    # for clarity — PATH alone would also work.
    export PATH="$VENV_DIR/bin:$PATH"
    export VIRTUAL_ENV="$VENV_DIR"
    exec gosu "$HOST_UID:$HOST_GID" \
        env PATH="$PATH" VIRTUAL_ENV="$VIRTUAL_ENV" \
            HOME="$HOME" UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
        "$VENV_DIR/bin/python" /opt/executor/server.py
fi

# Absolute last-resort fallback: no venv was created.
exec gosu "$HOST_UID:$HOST_GID" python /opt/executor/server.py
