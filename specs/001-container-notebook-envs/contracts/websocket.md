# WebSocket Contract

**Date**: 2026-04-10
**Endpoint**: `ws://localhost:8000/ws/notebooks/{notebook_id}`

## Connection Lifecycle

1. Client opens WebSocket connection to the notebook endpoint
2. Server verifies notebook exists; if not, closes with `4004`
3. Server ensures container is running; sends `container_state`
4. Client and server exchange messages until disconnect
5. On disconnect, execution in progress continues in the container;
   results are buffered and delivered on reconnect

## Message Format

All messages are JSON objects with a `type` field.

## Client → Server Messages

### Execute Cell

```json
{
  "type": "execute",
  "cell_id": "uuid",
  "code": "string"
}
```

Submits code for execution in the notebook's container. The server
responds with a `state` message (running), followed by zero or more
`output` messages, and concludes with a `state` message (completed
or errored) and optionally a `result` or `error` message.

### Interrupt Execution

```json
{
  "type": "interrupt",
  "cell_id": "uuid"
}
```

Sends SIGINT to the running process in the container for the
specified cell. The server responds with an `error` message
(KeyboardInterrupt) and a `state` message (errored).

## Server → Client Messages

### Cell Output

```json
{
  "type": "output",
  "cell_id": "uuid",
  "stream": "stdout|stderr",
  "text": "string"
}
```

Streamed in real-time as the container produces output. Each message
contains one or more lines of text.

### Cell Result

```json
{
  "type": "result",
  "cell_id": "uuid",
  "data": "string"
}
```

Sent when the executed expression has a displayable return value
(non-None). Analogous to Jupyter's `execute_result`.

### Cell Error

```json
{
  "type": "error",
  "cell_id": "uuid",
  "ename": "string",
  "evalue": "string",
  "traceback": ["string"]
}
```

Sent when execution raises an unhandled exception.

### Cell State Change

```json
{
  "type": "state",
  "cell_id": "uuid",
  "execution_state": "running|completed|errored",
  "execution_count": "integer"
}
```

Sent at the start and end of each cell execution. `execution_count`
is assigned when state becomes `running`.

### Container State

```json
{
  "type": "container_state",
  "status": "starting|ready|stopped|error",
  "message": "string (optional, present on error)"
}
```

Sent on WebSocket connect and whenever container state changes.

## Error Codes (WebSocket Close)

| Code | Meaning |
|------|---------|
| 4004 | Notebook not found |
| 4010 | Container failed to start |
| 4029 | Too many concurrent executions |
| 1000 | Normal closure |
| 1001 | Client going away |

## Execution Ordering

- Only one cell may execute at a time per notebook. If the client
  sends an `execute` message while another cell is running, the
  server queues it and processes sequentially.
- The client can interrupt the current execution and the queued
  cell will proceed next.
