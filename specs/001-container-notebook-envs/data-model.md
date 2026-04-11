# Data Model: Container-Based Notebook Environments

**Date**: 2026-04-10
**Source**: [spec.md](spec.md), [research.md](research.md)

## Entities

### Notebook

The primary user-facing document. Each notebook maps 1:1 to a
Docker container and a Docker named volume.

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| id | UUID | PK, auto-generated | Unique notebook identifier |
| name | string | 1-255 chars, unique | User-assigned display name |
| created_at | datetime | ISO 8601, immutable | Creation timestamp |
| updated_at | datetime | ISO 8601 | Last modification timestamp |
| cells | ordered list of Cell | Min 0 | Ordered cell collection |
| container_id | string | nullable | Docker container ID (null when no container exists) |
| volume_name | string | auto-generated | Docker volume name: `jredux-{id}` |

**Validation rules**:
- `name` MUST be non-empty and unique across all notebooks
- `cells` order is significant; position determines rendering order
- `updated_at` MUST be refreshed on any cell or metadata change

### Cell

An individual block within a notebook. Can be code or markdown.

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| id | UUID | PK, auto-generated | Unique cell identifier |
| cell_type | enum | `code`, `markdown` | Determines rendering and execution behavior |
| source | string | Any length | Cell content (code or markdown text) |
| outputs | ordered list of Output | Empty for markdown cells | Execution outputs |
| execution_count | integer | nullable, >= 1 | Incremental execution counter (null if never executed) |
| execution_state | enum | `idle`, `running`, `completed`, `errored` | Current execution state |

**Validation rules**:
- `cell_type` MUST be one of the defined enum values
- `outputs` MUST be empty for markdown cells
- `execution_count` MUST be null for markdown cells
- `execution_state` MUST be `idle` for markdown cells
- `execution_count` increments globally per notebook (not per cell)

### Output

A single output item produced by code cell execution.

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| output_type | enum | `stdout`, `stderr`, `result`, `error` | Output classification |
| content | string | Any length | Output text content |
| timestamp | datetime | ISO 8601 | When this output was produced |

**Validation rules**:
- `output_type` MUST be one of the defined enum values
- `error` type outputs include exception name, value, and traceback
  in the `content` field as structured text

### ContainerState

Runtime state of a notebook's Docker container (not persisted to
JSON — derived from Docker at runtime).

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| status | enum | `none`, `starting`, `ready`, `stopping`, `stopped`, `error` | Container lifecycle state |
| container_id | string | nullable | Docker container ID |
| host_port | integer | nullable | Mapped host port for executor TCP connection |
| error_message | string | nullable | Error details if status is `error` |

**State transitions**:
```
none → starting → ready → stopping → stopped
                     ↘ error
starting → error
```

- `none`: No container exists (new notebook or after deletion)
- `starting`: Container is being created/started
- `ready`: Container running, executor server accepting connections
- `stopping`: Graceful shutdown in progress
- `stopped`: Container exists but is not running
- `error`: Container failed to start or crashed

## Relationships

```
Notebook 1 ──── * Cell
Cell 1 ──── * Output
Notebook 1 ──── 1 ContainerState (runtime, not persisted)
Notebook 1 ──── 1 Docker Volume (external, managed by name convention)
```

## Storage Schema (JSON)

Each notebook is stored as a single JSON file at
`data/notebooks/{notebook_id}.json`:

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "My Analysis Notebook",
  "created_at": "2026-04-10T14:30:00Z",
  "updated_at": "2026-04-10T15:45:00Z",
  "cells": [
    {
      "id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
      "cell_type": "code",
      "source": "pip install pandas",
      "outputs": [
        {
          "output_type": "stdout",
          "content": "Successfully installed pandas-2.2.0",
          "timestamp": "2026-04-10T14:31:00Z"
        }
      ],
      "execution_count": 1,
      "execution_state": "completed"
    },
    {
      "id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8",
      "cell_type": "code",
      "source": "import pandas as pd\nprint(pd.__version__)",
      "outputs": [
        {
          "output_type": "stdout",
          "content": "2.2.0",
          "timestamp": "2026-04-10T14:32:00Z"
        }
      ],
      "execution_count": 2,
      "execution_state": "completed"
    }
  ]
}
```

## Index File

A lightweight index at `data/notebooks/index.json` for fast listing
without reading every notebook file:

```json
{
  "notebooks": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "name": "My Analysis Notebook",
      "created_at": "2026-04-10T14:30:00Z",
      "updated_at": "2026-04-10T15:45:00Z"
    }
  ]
}
```

Updated on every notebook create/rename/delete/save operation.
