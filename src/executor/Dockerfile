# --- CPU variant (default) ---
# Build with: docker build --build-arg PYTHON_VERSION=3.12 -t jupyter-redux-base:py3.12 -f src/executor/Dockerfile .
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS cpu

ENV PYTHONPATH=/env/lib
ENV PIP_NO_CACHE_DIR=1

RUN mkdir -p /env/lib

COPY src/executor/server.py /opt/executor/server.py

EXPOSE 9999

CMD ["python", "/opt/executor/server.py"]

# --- GPU variant ---
# Build with: docker build --target gpu --build-arg PYTHON_VERSION=3.11 -t jupyter-redux-base:py3.11-gpu -f src/executor/Dockerfile .
# Requires: NVIDIA Container Toolkit installed on host
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04 AS gpu

ARG PYTHON_VERSION=3.11
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        software-properties-common gpg-agent && \
    add-apt-repository -y ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        python${PYTHON_VERSION} python${PYTHON_VERSION}-venv && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python && \
    python -m ensurepip --upgrade && \
    ln -sf /usr/local/bin/pip${PYTHON_VERSION} /usr/local/bin/pip 2>/dev/null; true

ENV PYTHONPATH=/env/lib
ENV PIP_NO_CACHE_DIR=1

RUN mkdir -p /env/lib

COPY src/executor/server.py /opt/executor/server.py

EXPOSE 9999

CMD ["python", "/opt/executor/server.py"]
