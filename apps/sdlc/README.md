# boltzmann — execution workspace & interactive review

A development app for local use or a shared team server around **the published Temporal Agent Harness package**. Open a Git repository, investigate it or request a small change, approve a plan, watch the agent work, approve checks, inspect the patch, and accept or continue.

New workspaces run in **E2B sandboxes by default**. Existing local tasks keep their original workspace. See the sandbox setup below and [performance notes](PERFORMANCE.md) for the measured improvements and remaining latency costs.

New tasks use **Coding agent V2**: native OpenAI tools, bounded harness Code Mode, an implementer child, an independent reviewer child, and revision-bound verification. Existing tasks keep their V1 workflow and conversation. See [CODING_V2.md](CODING_V2.md) for the implemented scope, limits and test checklist; [AGENT_REVIEW.md](AGENT_REVIEW.md) preserves the original assessment.

**Milestone 3 is ready for testing:** a live execution workspace, per-file diffs, anchored human comments, persistent file review checkpoints, and targeted same-task follow-ups. Start with the [execution and review test guide](EXECUTION_REVIEW.md). GitHub delivery and CI feedback remain deferred until you test this milestone.

## Start

For team access, see [shared workspace setup, roles, and coordinated worktrees](COLLABORATION.md). New shared-mode tasks reuse one E2B sandbox per project, with separate task branches, owners, previews, and recoverable code checkpoints. Local startup below remains unchanged.

Requires Python 3.11+, `uv`, Node, `pnpm` (tested/pinned at 8.10.5), Git, and the Temporal CLI (`brew install temporal` on macOS). The target is macOS/Linux, one app instance per data directory. Shared mode supports multiple browser users on that server; see [multi-developer setup](COLLABORATION.md).

From the repository root:

```sh
./apps/sdlc/dev
```

Open the **launch URL printed in the terminal**. It unlocks the local application with an HTTP-only session cookie. The server binds to `127.0.0.1:8787`; its persistent Temporal server uses port `7333`. Keep the terminal running. Ctrl-C stops both owned processes; restarting restores tasks and approval gates.

**Temporal must be running for tasks to execute. You do not need to start it separately:** this launcher starts the persistent local Temporal server, the worker, and the web app together. Install the Temporal CLI first; no Docker, Temporal Cloud account, or separate harness server is needed for this local setup. Closing the browser is fine; stopping the launcher pauses execution until you launch it again. Ensure ports `8787` and `7333` are available (or use the port options below).

For a real task you also need a Git repository with at least one commit (a local checkout or a GitHub URL), and an API key with access to the exact API model ID you enter. Save the model key through **Settings** in your unlocked OS keyring, or export the configured environment variable **before launching** the app. The demo needs no model provider key, but uses E2B like other new workspaces. Checks may require project-specific runtimes/dependencies in the sandbox.

### E2B workspace setup

Create an [E2B API key](https://docs.e2b.dev/) and export it in the terminal that launches the app:

```sh
export E2B_API_KEY="your-e2b-api-key"
./apps/sdlc/dev
```

Settings shows whether the key is present. New task creation fails with a setup message if it is missing; there is no automatic host fallback. E2B authentication and availability are checked when creating the sandbox. E2B credentials stay in the app process, outside model prompts, task history, and the sandbox environment.

**Sandbox environments are selected automatically.** Before creating a task, the app inspects committed manifests, including nested packages, to choose a Node.js, Python, or combined runtime template. It detects npm/pnpm/Yarn/Bun from `packageManager` and lockfiles, exact Node versions from `.nvmrc`, `.node-version`, or Volta, and Python versions from `.python-version`. Common simple `engines.node` and `requires-python` declarations are also recognized. Defaults are Node.js 24 and Python 3.12 when a detected runtime has no recognized version declaration; complex ranges, conflicting monorepo versions, and runtimes other than Node/Python still require verification by the task's setup/check commands. Python template selection supports 3.11 and newer.

Matching runtime templates are built on demand through the [E2B template API](https://docs.e2b.dev/template/quickstart) and reused across repositories and app restarts. Builds install the runtime, package managers, Git, and Python needed by the harness, and verify that the tools run. They do **not** copy repository files or install repository dependencies into shared templates. Dependencies remain in the individual task's workspace. Shared-mode tasks have separate worktrees in the project's sandbox. First-time builds can add up to approximately 90 seconds to preparation; if a build fails or is still pending, the task uses E2B's general-purpose `base` environment, and a later task can reuse the completed build. Build requests and SDK calls have bounded waits, failures have a retry cooldown, and parallel requests share a local build lock. The UI shows preparation feedback and records the selected environment and fallback reason under **Session, plan & workspace details**.

No template selection or repository configuration is required. An optional custom `E2B_TEMPLATE` name/ID in the launch environment remains an operator override; unset it or use `auto` (also the legacy `base` value) for automatic selection. Existing sandboxes retain their environment, and forks inherit their parent's selected template. Build receipts live in `sandbox-templates/` under the app data directory, separated by E2B credential and endpoint. App-managed templates remain in E2B for reuse when tasks are deleted and can be removed through E2B; normal E2B build and sandbox charges apply.

The app uploads a bounded snapshot of committed HEAD, excluding secret/generated paths, symlinks, and Git history/configuration. Uncommitted and untracked source files stay local. The sandbox owns the task's files, Git baseline, dependencies, checks, and command home. File tools, manual edits, review, patch export, forks, and revision-bound verification all use that same sandbox. **Merge into project** remains an explicit, guarded write into the configured local source checkout.

The sandbox ID is saved under the app data directory in `sandboxes/<task-id>.json`. An app restart reconnects to that ID. Sandboxes auto-pause after their five-minute running timeout and resume on access, preserving files and installed dependencies. If a sandbox is lost or unreachable, the app reports the problem rather than creating an empty replacement or executing on the host. Keep the app data directory as well as the E2B sandbox to continue a task.

**Local mode and existing per-task sandboxes:** deleting a task while keeping its workspace pauses its sandbox and retains the descriptor. Selecting **Also permanently delete this task's isolated workspace** kills that task's E2B sandbox. **New shared-mode worktrees:** deletion stops only the task's preview and optionally removes its worktree/branch and checkpoints. The project sandbox stays available to other tasks and can pause when idle. Use **Coordinate → Sandbox → Recover sandbox** for explicit project-wide replacement from saved code checkpoints. Cleanup errors keep the task listed for retry. E2B's service charges apply. Repository size is limited to 50,000 regular files / 256 MB, and existing text/patch/command limits still apply.

For an explicit local-only development or offline demo session, launch with `SDLC_WORKSPACE_BACKEND=local ./apps/sdlc/dev`. That option allows new host workspaces; the UI labels them as local, and approved commands can access host files. Changing the default does not relocate existing tasks or turn E2B workspaces into local ones.

The script installs the independent Python app, builds its Svelte UI, and starts the stack. It uses `temporal-agent-harness[ui,openai-agents,code-mode]==0.4.0` from the lockfile by default, including Monty. After updating, restart with this script once to install the new dependency. Your existing environment-variable/keyring profile setup stays the same. To test **local harness changes**:

```sh
./apps/sdlc/dev --local-harness
```

This installs the harness checkout as an ordinary editable dependency into the **app's** virtual environment. No source-path injection, example imports, harness fork, or changes to the harness package are required. To include changes to the harness's own frontend, also build that UI (`pnpm --dir ui build`) before launching. Running `./apps/sdlc/dev` again restores the locked published dependency.

After dependencies and UI are built, the fast start is:

```sh
cd apps/sdlc
.venv/bin/sdlc --data-dir .local
```

Options: `--port 8788`, `--temporal-port 7334`, `--data-dir /your/private/path`, or `--external-temporal 127.0.0.1:7233`. The external server is neither started nor stopped by the app. This is a local development setup, not a hosted service deployment.

## Your first test

1. Configure E2B as above, select **Add repository → Choose folder**, and add an OpenAI model profile in **Settings**. Choose a small change in that repository and select **Start task**.
2. In **Execution**, select an agent to inspect its blueprint, files, or verification evidence. **Follow live** follows the latest active role; **Remaining** shows unfinished steps. At **Approve this blueprint**, refresh the browser or restart the app. The same decision should still be waiting.
3. Review and approve the blueprint, then review and approve the proposed verification commands. Inspect pass/fail evidence in **Checks**. In **Review**, select a file, click a line to comment, and mark the file reviewed. **Since last review** compares each file with its last reviewed contents.
4. Open **Harness** to see the embedded packaged debugger, model/tool events, replay, and observable state. boltzmann owns task controls; debugger mutations are blocked at the API boundary.
5. Open **Files** to inspect or manually edit a file when the task is paused or ready for review. Edits invalidate earlier check evidence. Use **Request fixes** in Review to prepare selected comments in **Continue this task**, then review the message and send it. A fresh blueprint requires approval. Comments stay open until you resolve them.
6. **Accept** records your review decision. Shared worktree tasks then use **Integration queue → Queue & test** to combine accepted patches in another E2B worktree, run combined checks/preview, and obtain renewed human acceptance before **Open pull request** publishes to GitHub. See [Stage 3](COLLABORATION.md#stage-3--reviewed-integration-implemented) for permissions and testing. Legacy/local **Merge into project** still applies the accepted text patch as uncommitted source-checkout changes through a journaled Temporal activity, preserving non-overlapping edits. **Patch** remains a download fallback.

For a read-only first task, choose **Ask the codebase**. Demo repositories and profiles are no longer offered in the interface; existing task history remains available.

New provider profiles and tasks support **OpenAI** with an explicit API model ID and a model-call budget. Use a model that supports structured output through the Responses API. Enter a key to save it in the OS keyring, or reference an environment variable exported in the launch terminal. The app does not load the harness repository's `.env` files. Removing a profile removes its saved key; active tasks must first be stopped. To change a profile, add its replacement and select it for the next message or new task.

New OpenAI tasks use the **OpenAI Agents SDK through the harness's packaged integration**, streaming through `/v1/responses`. Server-side response storage and separate OpenAI tracing export are disabled; boltzmann supplies its own task context. After restarting boltzmann, your existing OpenAI profile can be used for new tasks and connection tests without replacing the model or key. Tasks created before this migration retain their original activity implementation so their saved histories can replay. Older Anthropic/compatible profiles remain visible for those existing tasks, but cannot start new tasks or connection tests in this milestone.

Select **Settings → Model profiles → Test connection** after saving a profile. This makes one small streamed model request using the same OpenAI Agents SDK, model settings, instructions, and structured action schema as a task, with automatic retries disabled and a 45-second time limit. Provider charges may apply. It reads the credential from the running app's environment or OS keyring; no repository content is sent and no task or workspace is created. The probe runs outside a workflow and does not require a model activity. The result shows credential lookup failures, the endpoint, HTTP status, rejected parameter, and the provider's message with credential redaction. These diagnostic results are temporary and are not saved in SQLite or Temporal history. Success verifies a single structured response, not that every future task will succeed.

For an environment reference, export the key in the **same terminal that starts boltzmann**, then launch from the repository root:

```sh
export OPENAI_API_KEY="your-api-key"
./apps/sdlc/dev
```

In the profile, leave **API key** empty and set **Or environment variable** to `OPENAI_API_KEY`. An already running app cannot see variables exported later; stop it with Ctrl-C and restart it in that terminal. No script edits or separate diagnostic command are needed. Restart after app updates too, then refresh the browser.

If a task fails, **Activity** shows an error card with a specific category, captured HTTP status/recognized provider code when available, and recovery steps. API keys and raw provider responses are excluded. **Provider settings** opens profile setup; **Continue** restores the failed message into the follow-up composer in the same task. Review the request, profile, and budget, then press **Send message**; continuing is not an automatic retry. Runs made before detailed diagnostics were added retain their original history and show a notice when the exact cause was not captured.

### Temporal outages and shutdown

If progress queries report `Unavailable: shard status unknown`, inspect `.local/temporal.log`. This is a Temporal server error; a running app process does not establish server health. Persistent SQLite errors such as `interrupted (9)` can require restarting the local stack: stop with Ctrl-C, let shutdown finish, then run `./apps/sdlc/dev` again from the repository root with the same environment and data directory. Preserve `.local/` so task histories and workspaces can resume.

Shutdown drains SDK streams and closes cached workflow coroutines in their workflow context. New model activities allow up to three attempts to recover from interruptions such as `WorkerShutdown`; recognized provider and credential failures remain non-retryable. Activities scheduled by older versions retain their original retry policy. If an interrupted task has already failed, use **Continue** and **Send message** to retry in its preserved workspace.

## Continue a conversation

Required decisions stay in the action bar at the top of every task view, including while scrolling or reviewing files. Approve blueprints and commands, answer questions, resume paused work, or review failures from there. Decision dialogs keep their buttons visible while long details scroll. The **need attention** menu lists waiting tasks and the action each needs; it also works on small screens.

The workspace overview brings together the task composer, pending decisions, and recent tasks with their next actions. The interface uses shared graphite, blue, and amber design tokens across execution, reviews, settings, and dialogs. Overview and Settings remain accessible in the mobile navigation. See [the interface design notes](ui/DESIGN.md) for the visual direction and source skill.

Selecting a repository opens its task composer with the repository fixed, and filters the task list and its counts to that repository. Task history and search live in Overview and repository views; the left navigation contains repositories in stable alphabetical order. **New task** exposes a searchable repository popover: search by name, path, or GitHub remote, use the arrow keys and Enter to select, or choose **Add repository…**. Escape dismisses the popover while keeping the task composer open. **Overview** returns to tasks across all repositories.

**Add repository** opens a dialog from the sidebar, New Task selector, or Settings. **Choose folder** uses the native macOS folder picker, or Zenity/KDialog on Linux when available. If the desktop picker is unavailable, an in-app folder browser provides navigation without typing a path. Cancelling the native picker leaves the dialog open and adds nothing. Choosing a repository or one of its subfolders detects its Git root and remote, reuses an existing connection when present, and immediately opens its task composer. Connected repositories appear alphabetically, with shortened paths to distinguish checkouts. Opening a repository or task does not change their order.

In **Settings → Repositories**, use the remove button to hide a repository and its tasks. Removal preserves source files, task history, and sandboxes; running tasks keep running. Adding the same folder again restores the existing repository identity and its history.

Choose **Choose projects folder** once to find suggested repositories below that directory. The choice persists across restarts; **Change**, refresh, and **Forget projects folder** manage discovery without deleting files or connected repositories. Scans stop at Git roots, recognize worktrees, skip symlinks and hidden/dependency/build directories, and are limited to three levels, 1,500 directories, 200 suggestions, and a two-second scan budget. A missing folder or an incomplete scan is shown in the dialog. **Enter path** remains collapsed by default for manual entry. Folder browsing, discovery, and native picker requests all require the unlocked local app session; picker requests also use the existing origin and application-header checks.

In **Activity → Continue this task**, ask a follow-up question, refine the feature, or describe an issue to fix, then select **Send message** (`⌘/Ctrl Enter`). The task ID, Temporal workflow, workspace, prior activity, changed files, and check evidence stay together. **Accept** records review without closing the conversation. You can also continue failed or stopped tasks, including tasks created before this feature; their previous failure remains in Activity and Harness history.

Choose **Ask (read only)** or **Make changes**, a model profile, and a budget of **3–200 model calls for this message**. The default comes from the selected profile. Each message gets its own allowance; the sidebar shows both the current message's usage and lifetime task totals. A new change message always needs a fresh plan approval, and each command still needs approval. Later writes invalidate previous checks. Switching model profiles carries the retained conversation and repository context to that model.

You can draft while the agent works. Send after it finishes, or select Stop and wait for it to stop. At an approval or question, use the existing response controls first; at a pause, resume or stop. There is no queue or mid-action redirection in this milestone. Save or discard unsaved file edits before sending. Drafts are stored per task in this browser; accepted messages and execution settings are durable. If delivery is interrupted, boltzmann retries the same message ID after reconnect/restart to avoid duplicate execution, and shows an undelivered message if the agent rejects it.

The agent receives the original request and retained conversation/tool results, and is instructed to reread files before editing. Older context is bounded to 120,000 characters between messages by dropping whole older records; the sidebar reports when this happens. Full activity and harness history remain available. Long tasks still share one Temporal history; automatic summarization and continue-as-new are future work. For a separate experiment or a fresh context, **Fork task** creates another task from the current workspace with a short handoff summary.

## Preview a web app

For approved V2 changes, the implementer can call **prepare_and_test_preview**, and the controller also checks a detected web app before verification and review. This detects JavaScript `dev`/`start` scripts and static sites, prepares the package manager, installs dependencies, starts the app in the task’s existing E2B sandbox, and checks the public preview URL. Startup errors and bounded logs return to the agent for scoped repair; setup does not require a separate command approval. Ask tasks never automatically launch a preview. A failed preview check prevents the controller from reporting full verification.

The package manager comes from `packageManager` or the lockfile, with pinned versions honored. Missing Bun/pnpm/Yarn tooling is installed into a private sandbox directory using npm; modern Yarn uses Corepack. This follows the supported [Bun installation](https://bun.sh/docs/installation) and [Yarn Corepack](https://yarnpkg.com/corepack) paths. The sandbox template must provide Node.js and npm. Monorepo dependencies are installed at the detected lockfile root. Tooling, installation, startup, and response-check progress appear in the Preview tab.

You can also select **Start preview** in the **Preview** tab without choosing a command. **Advanced settings** is collapsed by default and allows overriding the command, directory, and port. A custom command replaces automatic setup and must run a foreground server bound to `0.0.0.0` on the selected port; `HOST` and `PORT` are provided. Vite detection configures the sandbox’s specific hostname. Non-JavaScript servers can use a custom command; automatic framework setup is currently limited to JavaScript apps and static HTML.

The preview appears inside the task after a successful HTTP response (2xx/3xx); error pages such as HTTP 500 do not count as ready. The agent also checks the public URL, beyond the local sandbox listener. These are HTTP smoke checks, not browser rendering or interaction tests. Use **Open in new tab** for a full browser view or apps that block embedding. A dev server with hot reload reflects workspace edits automatically; otherwise reload or restart. The agent can request a restart after changing dependencies or server configuration. Logs retain the latest 32 KB, with an 8 KB tail returned to the agent. Startup has a nine-minute limit. Starting a preview does not accept or merge changes, or replace the task’s other tests.

**Preview URLs are public to anyone who has the link while the server is running.** Preview setup is authorized as part of approved Change work, or by selecting Start preview yourself. Provider and E2B credentials are not inherited by the server. Apps that need their own environment/configuration must have it configured inside the sandbox. Sandboxes configured with private ingress are not supported by this direct-browser preview.

**Stop preview** stops installation/server processes and their children while preserving files. The agent respects this explicit stop; use Start preview to allow another run. Closing the tab or completing the task does not stop the preview. While the Preview tab is visible or the agent is verifying it, polling refreshes the sandbox’s five-minute timeout. Otherwise its existing auto-pause/resume policy applies. Deleting the task stops the preview before pausing a kept sandbox; deleting the workspace kills the entire sandbox. Legacy local workspaces cannot run previews through this feature.

## Delete a task

Open a task and select **Delete** next to its progress controls. Tasks that are working, paused, or waiting for approval must first be stopped; wait for **Stopped** before deleting. Tasks ready for review, accepted, failed, or stopped can be deleted, as can interrupted workspace preparations.

The confirmation dialog keeps workspace files by default and shows their location; an E2B workspace is paused when kept. Select **Also permanently delete this task’s isolated workspace** to kill the sandbox (or remove a legacy local clone) and its changes. The source repository and other task workspaces stay intact. Deletion removes the task from boltzmann and closes its Temporal execution; it cannot be continued from the app afterward. Temporal history, operation journals, and offloaded payloads remain under their existing retention policies. This is task cleanup, not erasure of every audit record.

boltzmann checks the current execution before deleting. If Temporal cannot be reached, the task and files are kept for retry. If workspace removal fails after the execution closes, the task stays listed so you can retry cleanup or choose to keep the remaining files.

## What is implemented

- A real V2 controller and role child workflows using `AgentWorkflowRunner` and `runner.state("sdlc", CodingState())`. All nested observable models use `HarnessState`. The workflow mutates state synchronously; the harness emits its native snapshots and patches. V1 remains registered for existing histories.
- Native OpenAI Agents SDK model activities via `OpenAIAgentsPlugin`, with the harness observer providing model streams, spans, and usage. Credential values are resolved inside model activities and do not enter workflow arguments. Connection testing uses the same SDK configuration.
- A custom progress UI showing controller stages, active roles/operations, blueprint requirements, check freshness, independent findings, changed files and aggregate usage. The controller derives verification from actual command receipts and a review of the same revision; a model summary alone cannot mark work verified.
- Durable plan and command gates, question/answer, bounded model calls, pause at action boundaries, stop, local acceptance, and ongoing conversations with per-message budgets, modes, and provider profiles.
- SQLite project/profile/task catalogue and archived full state snapshots. The browser refreshes authoritative snapshots every two seconds; reconnect replaces the whole document. **The first milestone deliberately uses full snapshots rather than the future SSE/patch projector.** The native harness debugger still consumes harness events directly.
- E2B workspaces seeded from committed HEAD, with a separate Git baseline, saved sandbox IDs, pause/resume, path/secret/generated-file exclusions, no symlink traversal, text-file limits, stale-hash rejection, atomic writes, and host-side operation journals. Existing local clones retain their backend. The source checkout's uncommitted/untracked files are not copied; the UI reports when the source was dirty.
- Automatic E2B web preview setup and HTTP smoke checks, missing package-manager installation, agent repair feedback, an embedded browser, public preview links, advanced overrides, bounded logs, and process cleanup.
- File inspection/manual editing, actual command exit codes/output, tracked and new text-file diffs, patch export, workflow-owned guarded merge into the configured source checkout, task history/search, draft persistence, keyboard shortcuts (`⌘/Ctrl K`, `⌘/Ctrl ,`), and opt-in desktop notifications. Merge activities use operation journals and per-project OS locks; completed receipts remain in observable Temporal state across app restarts.
- Optional shared accounts, project roles and invitations, per-person repository visibility/review checkpoints, attributed comments/decisions, and managed GitHub checkouts. See [stage 1 and the remaining stages](COLLABORATION.md).
- GitHub remote links for existing checkouts; immutable per-message provider configuration; credential references rather than secret values in workflow history and SQLite; same-origin local authentication and request checks.

## Deliberate limits of this milestone

**New E2B workspace commands run inside the sandbox after approval**, with network access and without inherited provider/E2B keys. Legacy local tasks and explicitly selected local mode still execute on the host. Verification commands have a 90-second/100 KB output limit, and uncertain interrupted commands are not automatically repeated. Preview servers use the separate lifecycle described above. Check recipes run sequentially because a recipe may alter the source or dependencies; before/after revision checks continue to invalidate stale evidence.

V2 uses the SDK's native tool loop, with structured proposals/results at role boundaries. The controller owns approvals, permissions and deterministic check execution. `src/sdlc_builder/integrations/` exposes a role-execution contract alongside the V1 decision contract, allowing future Pydantic AI integration. The old Pydantic AI dependency/activity remains for legacy replay; new OpenAI tasks use the OpenAI Agents SDK.

boltzmann includes a small serialization compatibility layer for harness 0.4.0: OpenAI fields use their wire aliases when written into Temporal payloads. This fixes structured responses whose `schema` metadata was otherwise stored as `schema_` and could be decoded as the wrong stream-event type. The harness source has the same fix; the compatibility layer lets the default published-package setup work before a new harness release. Existing histories keep their original decoding behavior. After updating, restart boltzmann and use **Continue → Send message** for an affected task; its old failure record is preserved.

The published package also needs a stream-observer adapter for function-call completion events that omit `name`. The harness source now takes the tool name from the opening item, and boltzmann adapts the observer's event copy for the published package. Without this fix, the provider can respond successfully but the task fails with `ResponseFunctionCallArgumentsDoneEvent ... has no attribute 'name'`. A passing connection test checks credentials, native tool calling, and structured output; it does not exercise the Temporal activity's harness observer. No key or profile change is needed for this error: restart the app and continue the affected task.

It is best suited to questions and focused text-file changes. Shared mode includes an integration queue, GitHub branch/PR publication, and commit-specific CI feedback. It has no semantic index, language server, interactive terminal, delete/rename tool, automatic browser interaction testing, deployment adapter, or automatic rollback. Deployment configuration is documented in the plan, not presented as a working control.

V2 stage status records controller progress; requirements still need your review. Check and review receipts identify the source revision. App edits invalidate evidence immediately; revision reads before/after checks and review, and again on acceptance, detect external edits. External edits are not continuously watched. Incomplete verification requires an explicit acceptance note. V1 retains its original progress semantics. Browser drafts are local to that browser; task state and evidence live in Temporal/SQLite.

Secrets entered through Settings never appear in application readback. Repository content and check output become task history and may be sent to the selected model; file exclusions are defensive defaults, not a guarantee that arbitrary source code contains no secrets. Keep the data directory private. The embedded Temporal dev server is suitable for local development only.

## Development and verification

```sh
cd apps/sdlc
.venv/bin/pytest -q                         # focused tests; live test skips by default
SDLC_INTEGRATION=1 .venv/bin/pytest tests/test_live.py tests/test_coding_live.py -q
.venv/bin/ruff check --config pyproject.toml src tests
pnpm --dir ui check
pnpm --dir ui build
```

The integration test launches its own isolated local stack and exercises the demo and native OpenAI SDK lifecycle, restarts at approval, stale approval rejection, actual test execution, patch preservation, observable-state event replay, manual-edit evidence invalidation, and task deletion across restart. It also covers same-task follow-ups, Ask/Change switching, repeated writes, fresh approval gates, cumulative usage, per-message budgets, and recovery from failures/cancellation. OpenAI tests run the actual SDK against a local Responses stream fixture; a separate replay test checks a saved pre-migration history. **No paid provider call is made by this suite.** Configure your own profile to test live model quality and account access.

Data under `.local/`: `app.sqlite3`, `temporal.sqlite3`, `temporal.log`, private workspace clones, operation journals, payload offload, command homes, and the local session token. These are ignored by Git. Back up the whole directory together. There is no automatic workspace or history cleanup yet.

If workspace preparation fails or is interrupted, **Retry setup** is available in the pinned action bar and error dialog, even before a worker has started the task. It keeps the same task and reconnects to the saved E2B sandbox, using the original uploaded snapshot. Empty workspace folders supplied by a template are supported. A completed initialization receipt recovers a lost response without resetting files or creating another sandbox. Populated folders without that receipt are preserved and require a new task; missing uploads and older forks without a saved continuation patch may also require a new task. Removing and re-adding a repository does not reset a failed workspace.

Useful feedback at this checkpoint: whether the approvals feel clear, whether progress makes the agent's next action obvious, whether Ask answers your real repository questions, whether the edit/check/review loop feels convenient, and which missing daily-development feature should come next.
