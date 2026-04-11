# REST API Contract

**Date**: 2026-04-10
**Base URL**: `http://localhost:8000/api`
**Content-Type**: `application/json`

## Notebooks

### List Notebooks

```
GET /api/notebooks
```

**Response** `200 OK`:
```json
{
  "notebooks": [
    {
      "id": "uuid",
      "name": "string",
      "created_at": "datetime",
      "updated_at": "datetime"
    }
  ]
}
```

### Create Notebook

```
POST /api/notebooks
```

**Request body**:
```json
{
  "name": "string (optional, default: 'Untitled Notebook')"
}
```

**Response** `201 Created`:
```json
{
  "id": "uuid",
  "name": "string",
  "created_at": "datetime",
  "updated_at": "datetime",
  "cells": []
}
```

**Errors**:
- `409 Conflict`: Notebook with this name already exists

### Get Notebook

```
GET /api/notebooks/{notebook_id}
```

**Response** `200 OK`:
```json
{
  "id": "uuid",
  "name": "string",
  "created_at": "datetime",
  "updated_at": "datetime",
  "cells": [
    {
      "id": "uuid",
      "cell_type": "code|markdown",
      "source": "string",
      "outputs": [
        {
          "output_type": "stdout|stderr|result|error",
          "content": "string",
          "timestamp": "datetime"
        }
      ],
      "execution_count": "integer|null",
      "execution_state": "idle|running|completed|errored"
    }
  ],
  "container_state": {
    "status": "none|starting|ready|stopping|stopped|error",
    "error_message": "string|null"
  }
}
```

**Errors**:
- `404 Not Found`: Notebook does not exist

### Update Notebook

```
PUT /api/notebooks/{notebook_id}
```

**Request body** (partial update — only include fields to change):
```json
{
  "name": "string (optional)"
}
```

**Response** `200 OK`: Full notebook object (same as GET).

**Errors**:
- `404 Not Found`: Notebook does not exist
- `409 Conflict`: Name already taken by another notebook

### Delete Notebook

```
DELETE /api/notebooks/{notebook_id}
```

**Response** `204 No Content`

Deletes the notebook file, stops and removes the Docker container,
and removes the associated Docker volume.

**Errors**:
- `404 Not Found`: Notebook does not exist

## Cells

### Add Cell

```
POST /api/notebooks/{notebook_id}/cells
```

**Request body**:
```json
{
  "cell_type": "code|markdown (default: code)",
  "source": "string (default: empty)",
  "position": "integer (optional, default: append to end)"
}
```

**Response** `201 Created`: Full notebook object with the new cell.

### Update Cell

```
PUT /api/notebooks/{notebook_id}/cells/{cell_id}
```

**Request body**:
```json
{
  "source": "string (optional)",
  "cell_type": "code|markdown (optional)"
}
```

**Response** `200 OK`: Full notebook object.

**Errors**:
- `404 Not Found`: Notebook or cell does not exist

### Delete Cell

```
DELETE /api/notebooks/{notebook_id}/cells/{cell_id}
```

**Response** `200 OK`: Full notebook object without the deleted cell.

**Errors**:
- `404 Not Found`: Notebook or cell does not exist

### Reorder Cells

```
POST /api/notebooks/{notebook_id}/cells/reorder
```

**Request body**:
```json
{
  "cell_ids": ["uuid", "uuid", "..."]
}
```

The `cell_ids` array MUST contain all cell IDs in the desired order.

**Response** `200 OK`: Full notebook object with reordered cells.

**Errors**:
- `400 Bad Request`: cell_ids list doesn't match existing cells
- `404 Not Found`: Notebook does not exist

## Container Management

### Start Container

```
POST /api/notebooks/{notebook_id}/container/start
```

Starts the notebook's container (creates it if it doesn't exist).

**Response** `200 OK`:
```json
{
  "status": "starting",
  "container_id": "string"
}
```

### Stop Container

```
POST /api/notebooks/{notebook_id}/container/stop
```

**Response** `200 OK`:
```json
{
  "status": "stopped"
}
```

### Container Status

```
GET /api/notebooks/{notebook_id}/container/status
```

**Response** `200 OK`:
```json
{
  "status": "none|starting|ready|stopping|stopped|error",
  "container_id": "string|null",
  "error_message": "string|null"
}
```
