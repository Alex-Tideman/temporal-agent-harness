# Multi-developer collaboration

## Stage 1 — shared project access (implemented)

One boltzmann server owns the project catalogue, task history, reviews, Git checkouts, and worker. Developers connect to that server from their browsers; they do not share a SQLite file or start separate app instances against the same data directory. The existing single-server SQLite store remains in place. Multiple app replicas and PostgreSQL are not part of this stage.

### Run a shared workspace

Configure `E2B_API_KEY` and the model credentials on the server. Start with:

```sh
./apps/sdlc/dev --public-url https://workspace.example.com
```

Put an HTTPS reverse proxy in front of `127.0.0.1:8787`, preserving the public Host header. `--public-url` must be an origin, without a path. Use `--host 0.0.0.0` only when your proxy/network arrangement needs it; a non-loopback bind requires shared mode. `SDLC_PUBLIC_URL` is the equivalent environment variable. HTTP is accepted only for localhost testing. Protect the server data directory and keep Temporal's ports private. The bundled Temporal development server is for development; configure `--external-temporal host:port` and the existing Temporal connection settings for a separately managed service.

Open the printed launch URL to create the initial owner account. This first account inherits the local projects and existing local review checkpoints. The setup token cannot sign into an established shared workspace. Subsequent access uses email and password. Passwords must have at least 12 characters and are stored with salted scrypt; opaque session tokens are stored as hashes. Sessions expire after 12 hours and are revoked on sign-out. Shared mode uses Secure, HttpOnly, SameSite=Strict cookies over HTTPS, same-origin mutation checks, and persistent sign-in rate limits. No email delivery, SSO, or self-service password recovery is included yet. Retain access to the initial owner account and back up the private data directory.

For a local-only desk, omit `--public-url` and `SDLC_PUBLIC_URL`. The existing launch-token flow still works. Shared mode is opt-in; updating the app does not publish your current workspace.

### Connect and share a project

1. Choose **Add repository**, paste `https://github.com/owner/repository`, and select **Connect**. The server creates a managed checkout; the connecting user becomes its project owner. Local mode also retains the folder picker. Shared users cannot browse the server's filesystem.
2. For private repositories, the operator sets `SDLC_GITHUB_TOKEN` before starting the server. Use a token restricted to the repositories the workspace should access, with read access to their contents. This is one server credential, not each user's GitHub identity. GitHub URLs must use HTTPS and cannot contain credentials. Credentials are passed through a temporary Git askpass helper, never saved in repository URLs or task input. Provider keys are excluded from Git's environment.
3. In **Settings → Repositories**, open the project's **members** button. Create an invitation for an email address and role; copy and share the resulting link yourself. Invitations expire after 48 hours, can be revoked, and can be accepted once. Existing users enter their existing password when joining another project.
4. Developers see only projects they belong to and those projects' tasks. **Hide repository** affects only their own sidebar. **Hidden repositories → Show repository** restores their view. Removing a person from the members dialog revokes project access on subsequent requests.

| Role | Project capabilities |
| --- | --- |
| Viewer | Read tasks, files, diffs, previews, comments, and team activity |
| Developer | Viewer access, plus create tasks, send follow-ups, operate sandbox tasks, edit files, and review/comment |
| Maintainer | Developer access, plus accept changes, merge into the server checkout, sync GitHub, and delete tasks |
| Owner | Maintainer access, plus invite people and change project membership |

At least one owner must remain. Workspace administration is separate from project ownership: only the initial workspace administrator manages model profiles or imports local server paths. Shared developers can use the configured model profiles without obtaining provider credentials. The embedded harness debugger is unavailable in shared mode because it does not enforce project membership.

New shared tasks require E2B and use the Stage 2 project sandbox described below, with a separate worktree and preview for each task. Existing tasks keep their original sandbox layout. Non-administrators cannot operate legacy local task workspaces; those tasks remain readable to project members. Start a new E2B task for shared work.

### Review and Git behavior

Comments show their author and resolver. File checkpoints belong to each reviewer, so one person's **Mark reviewed** never marks another person's review complete. Browser drafts are scoped to the signed-in account. **Team activity** records the initiator and outcome of task controls, acceptance, and merge requests. A lost worker response is marked **unconfirmed**, not treated as a successful decision. Shared acceptance and merge requests reject stale task versions and ask the reviewer to reopen the decision.

A managed GitHub checkout fetches its default branch before new tasks; maintainers can also sync it in Settings. Only a clean checkout on the configured branch is fast-forwarded. Local edits or diverging history stop the sync and preserve the checkout. Task sandboxes are seeded from the committed project state.

Shared worktree tasks use the Stage 3 integration queue below. **Merge into project for legacy/local tasks still applies accepted changes as uncommitted edits in the server's checkout.** That older action does not create a commit or publish a pull request; the operator must commit/export those changes before another GitHub sync. Shared task merge endpoints reject direct checkout writes and direct users to integration.

### Verification

The automated suite covers separate sessions, setup token rejection, expiry/revocation, role enforcement, cross-project reads and writes, debugger isolation, invitation reuse/expiry/revocation, personal hiding, concurrent independent review checkpoints, author attribution, stale decisions, Git credential handling, failed-clone cleanup, and preservation of dirty checkouts. GitHub network calls are mocked in these tests; no cloud sandbox or paid model is created.

## Stage 2 — coordinated task workspaces (implemented)

Stage 2 is automatic for **new tasks created in shared mode**. Restart the app after updating, keep `--public-url` / `SDLC_PUBLIC_URL` enabled, and start two new tasks in the same project. Existing task sandboxes and local mode retain their original lifecycle; there is no in-place migration.

### One project sandbox, separate task worktrees

The first task creates the project's E2B sandbox and selects a template from its committed manifests. Later tasks reuse that sandbox and template, avoiding another VM startup. A managed bare Git repository lives inside it; every task has a unique `sdlc/<task-id>` branch and `/home/user/worktrees/<task-id>` worktree. Each task imports the current committed project baseline, or the supported patch from a finished parent when forking. The original checkout is not modified by worktree creation.

Dependency installations run in each worktree. Command homes, preview tooling, caches, process groups, and preview ports are task-specific. Preview ports are reserved from 20000–48999; an advanced port override cannot claim another task's reserved port. Preview setup holds the same remote writer lock as code operations until the app answers successfully, or setup exits. Start commands must honor `$PORT` / the selected port.

Deleting a task with **Remove workspace** stops that task's preview and removes only its branch, worktree, and recovery files. Keeping the workspace retains its checkpoint and worktree for recovery; it does not pause other tasks. The project sandbox uses E2B's pause-on-timeout lifecycle and resumes on access. Deleting the last task does not destroy the VM; it can pause and be reused. **Recover sandbox** explicitly replaces the project's VM. [E2B persistence](https://docs.e2b.dev/sandbox/persistence) describes its pause/resume lifecycle.

### Ownership, handoffs, and coordination

The creator owns a new task. Only its owner can send follow-ups, approve its plan/commands, edit its files, or start/stop its preview. Other project developers can review and leave coordination notes; maintainers retain acceptance and merge permissions. A maintainer can stop an abandoned task, and its owner or a maintainer can hand it off to another current developer. Handoff requires a finished/stopped turn, no pending message, and no active writer. A pause alone is not an idle turn. Handoff checkpoints the code and stops that task's preview, then atomically records the new owner and attributed audit event. It does not automatically start an agent.

The task header shows its owner, isolated worktree, preview port, **Coordinate**, and **Hand off**. Coordinate highlights potential overlaps from saved changed files and declared blueprint scopes. The dialog has Coordination, Ownership, and Sandbox sections. Notes are persisted and attributed to a person or task agent. Agents on new shared tasks receive `project_coordination` and `coordinate_task` tools; listing files also includes current coordination context. Notes are read when the agent consults the board; they do not interrupt a running model call, trigger work, grant approvals, or change another worktree. Overlap detection is advisory, not conflict or integration verification.

There is one writer lease per worktree on the app server and a matching OS lock in the remote helper. Locks release when their owning process exits; no elapsed-time lease silently displaces a live operation. After an interrupted remote command, another operation fails while the old process still owns its lock. Command journals continue to prevent repeating an operation whose outcome is unknown. This remains a **single app server / worker deployment**, not a distributed lease service for multiple replicas.

### Checkpoints and recovery

Code checkpoints are saved on the app server after initial preparation, successful patches/manual edits, commands, and agent preview verification. A manually started preview is checkpointed when its status is polled after startup or when it is stopped. Handoff and reachable sandbox replacement also checkpoint first. Unchanged revisions skip archive/download work. Checkpoint transfer is checksum-validated and the current checkpoint is replaced atomically only after both artifacts are downloaded. Back up the private app data directory, including `workspace-checkpoints/`, `project-sandboxes/`, task descriptors, and SQLite, together.

Checkpoints retain allowed regular source files, untracked source files, deletions, binary contents, ordinary file permissions, index membership, and the original sanitized Git base. They exclude secrets, ignored/generated files (including dependencies), symlinks, processes, runtime state, and arbitrary task Git history/staging state. Unsupported sources or a changed snapshot fail checkpointing rather than claiming a complete backup. The limits are 50,000 files and 256 MB of source per task. A lost sandbox can only restore its last completed checkpoint.

To recover, finish or stop all project tasks, then open **Coordinate → Sandbox → Recover sandbox** as a maintainer/owner. The confirmation explains the project-wide effect and missing runtime data. The server verifies that every task is idle, checks the recovery artifacts, and confirms the old VM is gone before replacing it. Workflows remain blocked until all worktrees are restored and previous checks/acceptance have been invalidated. Retrying an unfinished recovery reuses its replacement and receipts; it does not overwrite a completed, subsequently changed worktree. Old preview URLs are discarded. Send a follow-up to reinstall dependencies, rerun checks, and restart previews. If the worker cannot confirm that tasks have stopped, recovery waits for connectivity.

Worktrees share a filesystem, Git object store, network, and process environment. They are **not a security boundary between untrusted contributors**: approved repository code can access the project's VM. Keep different trust groups in separate projects/sandboxes. Stage 3 adds reviewed integration without changing this trust boundary.

### Stage 2 acceptance checklist

1. Start two new tasks in one shared project, using two developer accounts. Their sandbox ID should match; branches, workspace paths, owners, and preview ports should differ.
2. Edit the same source file in both. Each review must show only that task's changes, and Coordinate should show the overlap. Send a note and confirm its author and incoming/outgoing direction from the other task.
3. Verify both web previews. They must serve their own code concurrently. Stop/delete one; the other must remain available.
4. Try operating the other developer's task. Plan/command approvals, edits, previews, and follow-ups should be unavailable until handoff. A maintainer should still be able to accept a reviewed task and queue it for integration.
5. Finish/stop a task, hand it off, and confirm the new owner can continue while the former owner retains review access. Repeated/stale handoff requests must be rejected.
6. Stop all tasks, save changes, and recover the project sandbox. Source revisions should be preserved, previews should be stopped, checks should be stale, and a previously accepted task should require renewed acceptance. Recovery while a task is active must be refused.

Automated tests use real Git worktrees and preview processes behind a local E2B transport double, plus API account/ownership tests and Temporal workflow tests. They do not establish E2B service availability; the real-cloud smoke tests are explicitly opt-in.

## Stage 3 — reviewed integration (implemented)

Select a repository in shared mode and open **Integration queue** in the top bar. Maintainers select accepted coding tasks and choose **Queue & test**. The server records immutable patches, source revisions, requirements, and authors; later edits to the source tasks cannot change the queued batch. Task versions and actual source hashes must match at capture. Inputs are bounded to twenty tasks, 500 KB of text per task, and 1 MB combined. Unsupported/binary/excluded changes fail capture instead of silently disappearing.

The first pending batch prepares automatically; later batches wait until it is published, cancelled, or rebuilt. Preparation fetches the configured GitHub target branch (or reads the local target), records its commit, and applies supported patches to a sanitized temporary checkout using only Git primitives. Conflicting file patches remain separate and attributed. The resulting seed is imported into a new worktree in the project's E2B sandbox, with its own branch, preview port, writer lease, and checkpoint. The server never runs repository installation/build/test scripts, and its managed checkout is not changed.

The integration is a normal durable Coding V2 task. Its coordinator and implementer can use **read_integration_input** to compare each accepted patch and its author, resolve conflicts in the integration worktree, install dependencies, run combined checks, and start the integration preview. Queueing authorizes initial integration planning and sandbox verification; follow-up blueprint changes still use the task's existing approval flow. The independent reviewer inspects the full combined diff. Source acceptance does not approve repairs or substitute for combined verification.

**Review required** stays visible above the queue's scrolling content. Open the integration task to inspect its code, findings, check receipts, and Preview tab. Accepting an integration requires successful verification and independent review for its current source hash; an acceptance note cannot waive failed/stale checks. Changes or sandbox recovery invalidate that evidence. The task owner can send a follow-up to repair problems; maintainers can review and accept after an ownership handoff.

### Publication and CI

After acceptance, choose **Open pull request** in the integration queue. This is an explicit publication action. The server checks the current workflow version, accepted source hash, verification/review revision, and target branch again. A moved target marks the batch stale; **Rebuild on latest target** creates another integration from the same immutable inputs, with fresh verification and acceptance. Existing reviews are preserved.

Publication creates a commit parented to the real target commit using a private Git index. It preserves target history and files excluded from the sandbox, applies only validated text patches, and stores the resulting commit plus approver before network delivery. The server creates a unique `boltzmann/integration-<id>` GitHub branch and opens a pull request. Retrying an uncertain response reconciles that same branch, commit, and PR; an existing branch with different contents is never overwritten. Editing is blocked once publication starts. **Check GitHub CI** shows commit statuses and check runs tied to the published commit, and indicates if the PR head or target moved. Large check lists are explicitly marked incomplete. Final merging happens on GitHub, under the repository's own branch protection and CI rules.

The operator's `SDLC_GITHUB_TOKEN` needs **Contents: read/write**, **Pull requests: read/write**, and access to read checks/statuses for the connected repository. Changes to workflow files may require GitHub's additional workflow permission. Tokens stay on the app server and are never sent into E2B. Projects imported only by local path can run integration checks/previews, but GitHub publication requires a GitHub-connected repository.

### Durability and test checklist

Back up `integrations/` together with the existing store and workspace checkpoints. Queue mutations use a per-project OS lock and atomic journal replacement. Preparation and task dispatch resume from saved inputs and workspace receipts after server restart. An ambiguous failure stops with a visible retry action; it does not recreate an empty sandbox or repeat an uncertain repository command. Published/cancelled integration tasks are retained as review history.

1. Accept two shared tasks, select both in **Integration queue**, and choose **Queue & test**. Confirm a third integration task appears automatically with its own worktree and preview.
2. Edit a source task after queueing; confirm the integration still uses the originally captured patch. Try queueing an unaccepted/stale revision and verify rejection.
3. Queue conflicting changes. Open the integration task, inspect the agent's repair and combined evidence, then review the preview. Failed checks must block acceptance even with a note.
4. Accept the verified integration and open its PR. Confirm the PR contains the reviewed changes, while the server checkout remains unchanged. Refresh GitHub CI and compare its reported commit.
5. Move the target branch before publication. The batch must require rebuilding and renewed review. Retry a publication with a lost response and confirm no duplicate branch/PR appears.
6. Repeat with a viewer/developer account: queueing, cancellation, rebuilding, and publication require a maintainer/owner. Ordinary task ownership rules still govern repair and follow-up work.

Automated coverage uses real Git/E2B transport doubles for immutable capture, conflicts, queue ordering, idempotency, exact commit construction, stale targets, publication recovery, access controls, and acceptance gates. GitHub calls are mocked and real E2B availability is not established by these tests. Individual task verification remains distinct from integration verification: two passing tasks can still conflict when combined.
