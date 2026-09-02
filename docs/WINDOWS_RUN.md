# Windows product runbook

The product launcher derives the project root from its own location. It is
safe to copy the prepared bundle to another directory and run it there.

## Start

From the project root, double-click `start.cmd` or run it from a terminal:

```text
.\start.cmd
```

The launcher loads `scripts\env.ps1`, uses the explicit standalone Python
under `runtime\python`, starts the local server on
`http://127.0.0.1:18765/`, waits for `/api/v1/health`, and opens the default
Windows browser. The server runs without a visible worker window. A repeated
start reuses a healthy recorded server. A stale PID file is removed; an
unrelated process occupying the port is reported instead of terminated.

The server records its own PID, port, and a private local shutdown token in:
`workspace\state\server.pid`. It does not install a Windows service and does
not modify the system PATH.

## Stop

```text
.\stop.cmd
```

Stop reads the recorded PID, verifies the recorded localhost endpoint is a
ChongZu health endpoint, and terminates that exact PID with `/PID /T /F` on
Windows. It never uses `taskkill /IM python.exe`. The PID state is removed
after a successful stop or when the server is demonstrably stale.

## Process a directory

Open **处理新目录**, paste an existing directory path, and start the task.
The path may contain Chinese characters and spaces. The browser does not
enumerate the Windows filesystem; the backend validates that the path exists
and is a directory, then calls the existing `process_source()` coordinator in
a background worker. The UI polls task state once per second.

The first process performs scan -> independent extraction -> deterministic
clean/profile -> catalog. A later process can reuse unchanged extraction and
cleaning identities. Unsupported files remain in the Registry and one bad file
does not cancel the rest.

## Portable release contents

A runnable release needs the standalone Python/runtime packages, OCR models,
`src`, `scripts`, `start.cmd`, `stop.cmd`, and the built `frontend/dist`.
`runtime/venv`, `runtime/node-dev`, `node_modules`, caches, source evidence,
DuckDB state, Parquet, logs, and secrets are development/local state rather
than required frontend runtime inputs. A release build must copy the required
runtime/model trees explicitly and rerun doctor, process, catalog, and network
checks after relocation.

The product server is localhost-only and core processing is offline. Phase 7B
is not run; an absent `.env` is normal and produces `NOT_CONFIGURED` rather
than a doctor failure.
