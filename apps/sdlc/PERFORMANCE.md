# Workspace performance

Measured on this development machine against commit `e2a745e`, using 300 tracked text files (40 lines each), one modified file, and a search with no matches. Both implementations returned identical results. These are single local runs, not estimates of model or E2B latency.

| Operation | Before | After | Ratio |
| --- | ---: | ---: | ---: |
| Diff | 11,104 ms | 77 ms | 144× |
| Literal search | 3,677 ms | 38 ms | 98× |

The old diff inspected every repository file and launched up to three Git processes per file. It now selects changed/untracked paths before inspecting content. Search previously ran `git check-ignore` for every file; it now uses the already filtered Git file list while retaining path, symlink, size, binary and secret checks. Regression tests assert bounded process counts as well as output behavior.

Reproduce from `apps/sdlc`:

```sh
.venv/bin/python tests/benchmark_workspace.py --baseline e2a745e --files 300
```

## Changes along the pipeline

| Step | Change | Remaining cost |
| --- | --- | --- |
| Workspace creation | One filtered archive upload; a standard-library helper avoids installing the app/provider stack in each VM. An optional `E2B_TEMPLATE` can include runtimes and dependencies. | E2B provisioning, upload size, and template startup; not yet measured against the live service. |
| Inspection/planning | `read_files` reads up to 20 related files in one activity/RPC; faster search and diff. The model can request multiple independent tools in one response. | Model generation and the number of inspection rounds. |
| Implementation | Multi-file patches and their before/after revisions run in one remote operation. SDK sandbox connections are reused. | Model output, source hashing, and necessary write serialization. |
| Verification | Revision check, approved command, and final revision are one sandbox operation. Dependencies and command home persist with the workspace. | Actual checks and explicit approval. Recipes remain sequential because they may modify files/dependencies. |
| Independent review | Diff only scans changed paths; a review summary fetches up to 500 file snapshot pairs in a single request. | Independent model review remains a required stage. |
| Task dispatch/progress | Up to eight tasks reconcile concurrently, with the same per-task lock protecting delivery/deletion. | Browser snapshots refresh every two seconds; this remains polling, not streaming. |
| Follow-up/restart | Reconnect to the saved sandbox ID, with pause/resume retaining files and dependencies. | Resume latency and context carried into subsequent model requests. |

Workspace preparation time is stored as `preparation_ms` on the task. V2 tool receipts store `duration_ms`; check durations also appear beside the exit code in Checks. INFO logs include task ID, operation and duration, without file contents or credentials. Model latency/usage is available in the existing Harness view. These measurements can distinguish model time, sandbox I/O, checks, and time waiting for approval before further tuning.

No end-to-end speedup is claimed yet. The filesystem benchmark removes a concrete source of overhead; tool batching depends on model behavior, and E2B introduces network latency. A real project run with `E2B_API_KEY` is the next measurement. If dependency installation dominates, build a project-specific E2B template. If generation dominates, compare model profiles on the same task while retaining verification and independent review.

## Validation

```sh
cd apps/sdlc
.venv/bin/pytest tests -q
SDLC_INTEGRATION=1 .venv/bin/pytest tests/test_coding_live.py tests/test_live.py -q
pnpm --dir ui check
pnpm --dir ui build
# Creates one real E2B sandbox and attempts cleanup in finally:
SDLC_E2B_LIVE=1 E2B_API_KEY="your-key" .venv/bin/pytest tests/test_e2b_live.py -q
```

The regular suite uses explicit local fixtures and an E2B transport double that executes the actual standalone helper. It covers remote reads/writes, checks, excluded uploads, restart/reconnect, review, fork, merge, cleanup, lost command replies, and failures without host fallback. The real E2B smoke test is skipped unless explicitly enabled with a key.
