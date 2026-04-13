#!/usr/bin/env bash
# Executor entrypoint.
#
# Runs as root for a moment to create a user matching the host uid/gid,
# chown the paths that must be writable (the /env volume + the user's
# home directory with its cache mounts), then `exec gosu` into the
# executor server, dropping privileges permanently. This keeps files
# written on host bind mounts (e.g. /shared/artifacts) owned by the
# host user so the user can touch them from a normal shell without sudo.
#
# If JREDUX_HOST_UID is unset or 0 we skip the whole dance and run
# as root — handy for quick local experiments.

set -e

HOST_UID="${JREDUX_HOST_UID:-0}"
HOST_GID="${JREDUX_HOST_GID:-0}"

if [[ "$HOST_UID" == "0" ]]; then
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
    /home/jredux/.cache/torch

chown -R "$HOST_UID:$HOST_GID" /home/jredux
# /env is a named volume; the chown is cheap on an empty volume and
# idempotent on subsequent starts.
chown -R "$HOST_UID:$HOST_GID" /env 2>/dev/null || true

export HOME=/home/jredux

exec gosu "$HOST_UID:$HOST_GID" python /opt/executor/server.py
