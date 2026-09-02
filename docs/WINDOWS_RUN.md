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

## Search and query

After processing, use **数据检索** to search filenames, catalog metadata,
columns, bounded profile samples, and TextChunks. The command-line equivalent
is:

```text
.\chongzu.cmd search "北京大学" --type text --limit 20
```

Use **数据查询** only after selecting one or more table assets. The service
shows aliases such as `t1` and runs read-only SQL in a private in-memory
connection. It rejects external file functions, extensions, writes, system
catalog access, and unselected relations; no Registry or source path is
available to the query. The selected input is bounded to 50,000 total rows and
is rejected above that limit rather than silently truncated. Search and SQL
are local deterministic features and do not call the semantic provider.

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

## Build and verify a release candidate

Development only, from the repository root:

```powershell
.\scripts\build_release.ps1
runtime\venv\Scripts\python.exe scripts\run_phase10_acceptance.py
```

The builder requires a clean Git tree, uses an explicit allowlist, and writes
`release\ChongZu-0.1.0-rc1-win-x64`, its ZIP, SHA-256 sidecar, third-party
manifest, and local `licenses/` evidence. The bundle contains only the standalone
runtime, production packages/models, source, scripts, docs, and built
`frontend\dist`; it does not contain `runtime\uv`, `runtime\venv`,
`runtime\node-dev`, Node modules, caches, tests, development workspace state,
or `.env`. The acceptance harness copies the directory to a Chinese/space
path, extracts the ZIP to another one, and exercises both through `start.cmd`
and the localhost API. Release output is ignored by Git.
