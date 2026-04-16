# --- CPU variant (default) ---
# Build with: docker build --build-arg PYTHON_VERSION=3.12 -t jupyter-redux-base:py3.12 -f src/executor/Dockerfile .
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS cpu

RUN apt-get update && \
    apt-get install -y --no-install-recommends git gosu sudo build-essential && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Let the jredux user run apt-get (and nothing else) as root without
# a password, so notebook cells can `apt install <pkg>` to pick up
# system dependencies. The user is created at container startup by
# entrypoint.sh, but sudoers resolves names at invocation time so
# referencing it here is fine.
RUN echo 'jredux ALL=(root) NOPASSWD: /usr/bin/apt-get' > /etc/sudoers.d/jredux-apt && \
    chmod 440 /etc/sudoers.d/jredux-apt

# uv: fast pip replacement. `pip install` cells route through
# `uv pip install` when uv is present (with a binary-level fallback
# to plain pip if it ever isn't).
RUN pip install --no-cache-dir uv

# ruff: fast Python formatter/linter. Backed by the /format endpoint
# so cells can be reformatted via Shift-Alt-F.
RUN pip install --no-cache-dir ruff

ENV PYTHONPATH=/env/lib:/env/files
ENV PIP_NO_CACHE_DIR=1

RUN mkdir -p /env/lib /env/files /env/repos /home/jredux

COPY src/executor/server.py /opt/executor/server.py
COPY src/executor/entrypoint.sh /opt/executor/entrypoint.sh
RUN chmod +x /opt/executor/entrypoint.sh

EXPOSE 9999

ENTRYPOINT ["/opt/executor/entrypoint.sh"]

# --- GPU variant ---
# Build with: docker build --target gpu --build-arg PYTHON_VERSION=3.11 -t jupyter-redux-base:py3.11-gpu -f src/executor/Dockerfile .
# Requires: NVIDIA Container Toolkit installed on host
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04 AS gpu

ARG PYTHON_VERSION=3.11
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        software-properties-common gpg-agent git gosu sudo build-essential && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python${PYTHON_VERSION}-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python && \
    python -m ensurepip --upgrade && \
    ln -sf /usr/local/bin/pip${PYTHON_VERSION} /usr/local/bin/pip 2>/dev/null; true

# Same sudoers grant as the cpu target — see the note there.
RUN echo 'jredux ALL=(root) NOPASSWD: /usr/bin/apt-get' > /etc/sudoers.d/jredux-apt && \
    chmod 440 /etc/sudoers.d/jredux-apt

# uv: fast pip replacement. See the note on the cpu target.
RUN pip install --no-cache-dir uv

# ruff: same note as cpu.
RUN pip install --no-cache-dir ruff

ENV PYTHONPATH=/env/lib:/env/files
ENV PIP_NO_CACHE_DIR=1

RUN mkdir -p /env/lib /env/files /env/repos /home/jredux

COPY src/executor/server.py /opt/executor/server.py
COPY src/executor/entrypoint.sh /opt/executor/entrypoint.sh
RUN chmod +x /opt/executor/entrypoint.sh

EXPOSE 9999

ENTRYPOINT ["/opt/executor/entrypoint.sh"]
