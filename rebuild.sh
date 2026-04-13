#!/usr/bin/env bash
set -e

echo "==> Killing stale env containers..."
docker ps -aq --filter "name=jredux-env-" | xargs -r docker rm -f

echo "==> Rebuilding CPU executor image..."
docker build --no-cache --target cpu -t jupyter-redux-base:latest -t jupyter-redux-base:py3.11 -f src/executor/Dockerfile .

echo "==> Rebuilding GPU executor image..."
docker build --no-cache --target gpu -t jupyter-redux-base:py3.11-gpu -f src/executor/Dockerfile .

echo "==> Done. Run 'docker compose up' to start the server."
