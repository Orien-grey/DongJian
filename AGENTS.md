# Repository Instructions

These instructions apply to the entire DongJian repository.

Global Codex instructions remain in effect. When this file is more specific,
follow this file for DongJian-specific product and architecture constraints.

# 1. Product Mission

DongJian is a fully relocatable Windows x64 local workbench for organizing and
processing files from one scientific research project.

Its primary processing jobs are:

- table extraction
- text extraction

A user-configured OpenAI-compatible LLM service may provide semantic naming,
categorization, field explanations, summaries, and difficult quality judgments.

Deterministic extraction, storage, provenance, and core correctness must not
depend on an LLM.

The current development root is:

`E:\Desktop\DongJian`

Never hard-code that absolute path into relocatable runtime behavior. Runtime
paths must derive from the project or launcher location.

# 2. Supported Production Environment

Production supports Windows x64 only.

The portable production bundle must not require:

- administrator rights
- WSL
- Docker
- Conda
- system Python
- system package managers
- external database services
- required user-level caches

Project-required Python, packages, native tools, models, caches, temporary
files, and runtime state must remain inside the project bundle.

Do not fall back to system executables, PATH-installed tools, user caches, or
implicit downloads when a required project-local component is missing.

Missing required local components should fail clearly with an actionable
message.

Do not add compatibility code for unsupported platforms or environments unless
the current task explicitly changes the supported platform contract.

# 3. Network and LLM Boundary

The core workflow is offline.

The only intended processing-time network target is an OpenAI-compatible LLM
base URL explicitly configured by the user.

Do not introduce:

- public endpoint fallback
- automatic model downloads
- automatic package downloads
- hidden cloud services
- automatic embedding services
- network fallback when a local component fails

LLM, vision, embedding, DeepSeek, Qwen, or OpenAI-compatible calls must not run
unless the user has explicitly configured and authorized them for the current
task or acceptance step.

Model selection must remain configuration-driven.

LLM output may create or suggest semantic metadata, quality findings, or user
review actions. It must not silently overwrite deterministic extracted data.

# 4. Processing Boundary

The currently supported first-stage business formats are:

- CSV
- TSV
- XLS
- XLSX
- PDF
- JPG
- JPEG
- PNG
- DOC
- DOCX
- PPT
- PPTX
- TXT

The registry may discover other files, but formats outside the supported list
remain cataloged as unsupported.

Unsupported files must not be deleted, moved, or converted into a whole-batch
failure.

Do not add business extractors for HTML, CSS, XML, JavaScript, archives,
executables, or unknown binary formats unless the current task explicitly
changes the product boundary.

Table extraction and text extraction are independent capabilities.

A single file may produce:

- zero or more TableAsset records
- zero or more TextAsset records

Do not model table extraction and text extraction as mutually exclusive.

PDF, images, DOC/DOCX, and PPT/PPTX may be dual-extraction candidates.

# 5. Core Architecture Contracts

Keep these layers conceptually separate:

- file registry
- processing policy
- table extraction
- text extraction
- deterministic normalization
- semantic enrichment
- catalog persistence
- future search and analysis

Preserve the existing:

`raw -> normalized -> semantic`

boundary.

Normalized output must not overwrite raw extraction.

Semantic metadata must remain a separate suggestion/review layer and must not
overwrite raw or normalized content.

Source files are read-only input evidence.

Do not modify source files.

Derived artifacts belong below `workspace/`.

`workspace/input/` is immutable evidence.

Keep the embedded catalog at:

`workspace/state/registry.duckdb`

DuckDB stores catalog metadata, provenance, run state, and query state.

Large extracted table data belongs primarily in Parquet and may be queried by
DuckDB.

The existing catalog contracts include:

- files
- contents
- scan_runs
- extraction_runs
- table_assets
- text_assets
- text_chunks
- semantic_metadata
- quality_issues

Do not populate fake/demo business records in a real registry.

# 6. Provenance and Hashing

Preserve the provenance model already implemented by the repository.

Existing asset identity and provenance may depend on:

- source file_id
- existing content SHA-256
- sheet/page/section coordinates where applicable
- extractor identity/version
- extraction run identity

Do not remove or redesign an existing provenance contract unless the current
task explicitly requires it.

However, do not expand hashing beyond the existing contract.

Do not introduce new:

- file hashes
- checksums
- fingerprints
- integrity passes
- duplicate hash systems
- alternate content-addressing schemes

merely for additional safety.

Do not repeatedly hash unchanged large files.

If the existing registry already has a valid content hash that satisfies the
current operation, reuse it.

Do not add a second hash computation merely to verify the first one.

Do not place unnecessary whole-file hashing on a latency-sensitive request,
render, or repeated processing path.

If a task appears to require new hashing behavior, first establish the concrete
repository requirement that makes it necessary.

# 7. Runtime and Dependency Direction

Production Python is project-local CPython 3.11.15 under the repository runtime.

Production packages must remain project-local.

Do not make the portable runtime depend on system Python, Conda, WSL, Docker,
or user-installed packages.

Current structured-data direction uses project-local:

- python-calamine
- Polars
- DuckDB
- Parquet

Current PDF native-text profiling uses PyMuPDF where already implemented.

OCR and table-extraction dependencies must remain project-local, offline, and
explicitly provisioned.

Do not add major dependencies merely because they provide a convenient helper.

Do not add Pandas, PyArrow, OpenPyXL, Java/Tika, Unstructured, Spark, Ray,
Kubernetes, Docker, or other large frameworks unless the current task
explicitly changes the approved technology direction and there is concrete
evidence the existing stack cannot satisfy the requirement.

GMFT, Docling, and similar heavy alternatives are comparison candidates, not
automatic default dependencies.

Do not install or download packages or models while performing architecture,
code-review, or planning-only tasks.

# 8. Implementation Philosophy

Implement the smallest direct change that solves the current task.

Prefer:

- existing project patterns
- existing interfaces
- direct code
- deterministic behavior
- bounded local work
- explicit errors

over new abstractions or generalized machinery.

Do not:

- redesign neighboring modules
- perform unrelated cleanup
- rename unrelated code
- reformat unrelated files
- create generalized helpers for one use
- create compatibility layers without a current requirement
- create extension points for hypothetical future work
- introduce configuration merely to make a one-off behavior configurable
- add fallback chains merely to make failure less visible
- add speculative caching
- add speculative concurrency
- add speculative retries
- add speculative background jobs

A bug fix does not imply a surrounding refactor.

A small feature does not imply a framework redesign.

Once the concrete code path and root cause are understood, stop broad
repository exploration and implement the required change.

# 9. Defensive Coding Policy

Defensive logic must be evidence-based.

Do not add behavior for a failure merely because it is theoretically possible.

Unless the current task, existing code, logs, a reproducible failure, or an
explicit repository contract demonstrates the need, do not add:

- fallback behavior
- broad try/except
- broad catch handlers
- silent error swallowing
- redundant null checks
- redundant existence checks
- retry loops
- duplicate validation
- compatibility branches
- hypothetical edge-case branches
- silent default values
- recovery paths for unsupported states

Validation is most appropriate at real boundaries such as:

- user input
- filesystem/process boundaries
- external network calls
- untrusted external files

Trust established internal invariants and repository contracts unless evidence
shows they are violated.

Prefer a clear failure over silently masking an unexpected internal state.

If additional defensive logic seems necessary, first determine:

1. the exact concrete failure being prevented;
2. the evidence in the current repository that the failure can actually occur;
3. the smallest change needed to handle it.

If there is no concrete evidence, do not add the extra logic.

# 10. Performance and Responsiveness

Do not solve local problems with global computation.

Avoid introducing:

- repeated repository scans
- repeated directory scans
- repeated parsing of unchanged files
- repeated hashing
- unnecessary full-dataset passes
- eager preprocessing of unrelated data
- expensive synchronous backend request work
- expensive frontend render work
- blocking initial UI rendering
- unnecessary serialization of large datasets

Prefer bounded work over whole-system work.

Keep frontend interactions responsive.

Do not add caching, indexing, parallelism, background processing, batching, or
worker complexity merely as theoretical optimization.

Add such mechanisms only when an actual requirement or measured bottleneck
justifies them.

# 11. Concurrency

Keep existing bounded concurrency behavior where the repository already
requires it.

Do not introduce new concurrency, worker pools, queues, locks, retry systems,
or backpressure mechanisms for a task that does not require them.

When modifying an existing concurrent path, preserve its established bounds and
interfaces.

Do not generalize a local concurrency fix into a repository-wide scheduling
system.

# 12. Error Isolation and Recovery

Preserve existing per-file failure isolation where already implemented.

One corrupt or unsupported input should not unnecessarily fail unrelated files.

Do not redesign recovery, resumability, interruption handling, or checkpointing
unless the current task concerns those behaviors.

Do not add new recovery layers simply because interruption is theoretically
possible.

Use the repository's existing task/run-state mechanism rather than inventing a
second one.

# 13. Testing Policy

Do not automatically create tests for every modification.

Add or modify tests only when:

- the current task explicitly requests them;
- a regression test is clearly necessary to reproduce the bug being fixed;
- an existing test contract must be updated;
- the task's acceptance criteria explicitly require tests.

When a test is needed, write the smallest focused test covering the concrete
behavior.

Do not automatically create test matrices for:

- corruption
- interruption
- unsupported formats
- extension mismatch
- retries
- fallback decisions
- concurrency
- provenance
- relocation
- every neighboring edge case

unless the current task actually concerns that behavior.

Use small synthetic fixtures when a fixture is necessary.

Do not generate large test datasets merely for completeness.

# 14. Verification Policy

Verification must be proportional to the modification.

Default completion verification is one narrow check appropriate to the touched
code, such as:

- Python syntax compilation
- import check
- focused type check
- frontend TypeScript compile check for the touched area
- a single directly relevant test when such a test already exists

Do not automatically run:

- the entire backend test suite
- the entire frontend test suite
- full repository builds
- full portable builds
- full regression suites
- lint across the repository
- repository-wide type checking
- static-analysis suites
- integration suites
- E2E suites
- coverage
- security scans
- dependency audits
- benchmarks
- performance profiling
- relocation acceptance
- package integrity verification
- whole-runtime verification
- SHA-256 verification passes

unless the current task explicitly requires one of them.

Run the narrow required verification once.

If it passes, stop.

Do not run another successful verification merely:

- to be safe
- for completeness
- as a final audit
- because the task is about to finish

If the narrow check fails:

1. fix that concrete failure;
2. rerun the same relevant check;
3. broaden verification only if the failure itself provides evidence that
   broader checking is necessary.

Do not escalate automatically from a focused change to a full test/build cycle.

If no useful narrow automated verification exists, inspect the touched code and
report that no narrow automated check was available. Do not substitute an
expensive repository-wide command merely so that some check was run.

# 15. Acceptance and Release Work

Normal implementation tasks are not release acceptance tasks.

Do not automatically run:

- portable packaging
- relocation verification
- doctor scripts
- full acceptance matrices
- release manifests
- checksum generation
- full dependency validation
- complete runtime smoke tests

after ordinary code changes.

Run release-level validation only when the user explicitly asks for:

- acceptance
- RC validation
- packaging
- release preparation
- deployment validation
- portable-bundle validation

For an explicit release or acceptance task, perform only the acceptance steps
actually requested.

# 16. Investigation Budget

Investigate only enough of the repository to correctly solve the task.

Start with the relevant path.

Once the root cause, existing contract, and required modification are clear:

STOP EXPLORING.

Do not continue searching the repository for:

- similar theoretical bugs
- neighboring cleanup opportunities
- unused code
- style inconsistencies
- possible future failures
- unrelated TODOs

unless the task explicitly requests a broader audit.

Do not turn a focused coding task into a repository review.

# 17. Research and External Patterns

Prefer existing DongJian patterns first.

For a non-trivial new UX, API, architecture, workflow, or product-design
decision, briefly study how established products, mature open-source projects,
or official frameworks solve the same problem before inventing a new design.

Prefer proven conventions when they fit DongJian.

External research must remain bounded:

- use a small number of strong references;
- focus only on the current design question;
- stop when a suitable proven pattern has been identified;
- do not perform external research for routine bug fixes or straightforward
  implementation tasks.

Research exists to reduce unnecessary invention, not to delay implementation.

# 18. Scope and Stop Condition

The user's current task defines the scope.

Do not silently expand it.

When the requested behavior has been implemented and the narrow required
verification has passed, the task is complete.

Then stop.

Do not continue with:

- extra cleanup
- extra refactoring
- extra robustness
- additional fallback behavior
- additional edge-case handling
- additional tests
- additional documentation
- additional optimization
- another verification pass
- unrelated fixes

Report the change, the narrow verification performed, and any concrete
remaining limitation directly relevant to the task.

Do not solve hypothetical future problems as part of the current task.