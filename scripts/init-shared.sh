#!/usr/bin/env bash
# Create the host-side directories that get bind-mounted into every
# env container at /shared/datasets (ro) and /shared/artifacts (rw).
#
# Run once after clone, or any time you want to reset defaults. Safe
# to re-run; existing directories are left alone.
#
# Defaults live under ~/jupyter-redux/. Override by setting
# JREDUX_DATASETS_PATH / JREDUX_ARTIFACTS_PATH before running.

set -e

DATASETS_PATH="${JREDUX_DATASETS_PATH:-$HOME/jupyter-redux/datasets}"
ARTIFACTS_PATH="${JREDUX_ARTIFACTS_PATH:-$HOME/jupyter-redux/artifacts}"

mkdir -p "$DATASETS_PATH" "$ARTIFACTS_PATH"

# Ensure the current user owns them — important so env containers
# running as host uid can write to /shared/artifacts without sudo.
chown -R "$(id -u):$(id -g)" "$DATASETS_PATH" "$ARTIFACTS_PATH" 2>/dev/null || true

cat <<EOF
Shared paths ready:
  datasets   (ro in container as /shared/datasets):  $DATASETS_PATH
  artifacts  (rw in container as /shared/artifacts): $ARTIFACTS_PATH

Drop raw datasets into $DATASETS_PATH/<dataset_name>/ — they will
appear at /shared/datasets/<dataset_name>/ in every notebook.

Write generated artifacts from notebooks to /shared/artifacts/<project>/
and they will land in $ARTIFACTS_PATH/<project>/ on the host.
EOF
