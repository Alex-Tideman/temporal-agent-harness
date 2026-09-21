<script lang="ts">
  import { onMount } from "svelte";
  import {
    GitMerge,
    X,
    ArrowUpRight,
    RefreshCw,
    Check,
    Layers,
  } from "@lucide/svelte";
  import { api, type Task, type Project, type Profile } from "./types";

  interface Batch {
    id: string;
    status: string;
    error: string;
    created_at: number;
    tasks: { task_id: string; revision: string }[];
    task?: Task;
    task_id?: string;
    target_branch?: string;
    target_commit?: string;
    conflicts: { task_id: string; path: string }[];
    publication?: {
      revision: string;
      commit: string;
      approved_by: { name: string };
    };
    pull_request?: { url: string; number: number };
  }
  interface CI {
    state: string;
    head_current: boolean;
    target_current: boolean;
    truncated: boolean;
    pull_request_state: string;
    checks: { name: string; status: string; conclusion?: string }[];
  }
  let {
    project,
    tasks,
    profiles,
    onselect,
    onrefresh,
  }: {
    project: Project;
    tasks: Task[];
    profiles: Profile[];
    onselect: (task: Task) => Promise<void>;
    onrefresh: () => Promise<void>;
  } = $props();
  let dialog = $state<HTMLDialogElement>();
  let batches = $state<Batch[]>([]);
  let selected = $state<string[]>([]);
  let profileId = $state("");
  let busy = $state(false);
  let loading = false;
  let error = $state("");
  let canManage = $state(false);
  let ci = $state<Record<string, CI>>({});
  let attempt = $state("");
  let selectionVersions = $state<Record<string, number>>({});
  let eligible = $derived(
    tasks.filter(
      (t) =>
        t.project_id === project.id &&
        !t.integration_id &&
        t.snapshot?.value.status === "accepted" &&
        t.snapshot.value.mode === "change",
    ),
  );
  let nextAction = $derived(
    batches.find(
      (batch) =>
        !["published", "cancelled", "superseded"].includes(batch.status) &&
        (batch.error ||
          ["review", "accepted", "failed"].includes(
            batch.task?.snapshot?.value.status || "",
          )),
    ),
  );
  const base = () => `/projects/${project.id}/integration`;
  const closed = (batch: Batch) =>
    ["published", "cancelled", "superseded"].includes(batch.status);
  const label = (batch: Batch) =>
    batch.status === "testing"
      ? batch.task?.snapshot?.value.status === "accepted"
        ? "Ready to publish"
        : batch.task?.snapshot?.value.status === "review"
          ? "Review required"
          : batch.task?.snapshot?.value.focus || "Preparing checks"
      : {
          queued: "Queued",
          preparing: "Preparing worktree",
          publishing: "Confirm publication",
          published: "Pull request opened",
          stale: "Target changed",
          cancelled: "Cancelled",
          superseded: "Rebuilt",
        }[batch.status] || batch.status;
  async function load() {
    if (loading) return;
    loading = true;
    try {
      const result = await api<{ items: Batch[]; can_manage: boolean }>(base());
      batches = result.items;
      canManage = result.can_manage;
    } finally {
      loading = false;
    }
  }
  export async function show(taskId = "") {
    error = "";
    selected = taskId && eligible.some((t) => t.id === taskId) ? [taskId] : [];
    selectionVersions = Object.fromEntries(
      eligible.map((t) => [t.id, t.version]),
    );
    profileId =
      tasks.find((t) => t.id === taskId)?.profile_id ||
      profiles.find((p) => p.available !== false)?.id ||
      "";
    attempt = "";
    dialog?.showModal();
    try {
      await load();
    } catch (e) {
      error = (e as Error).message;
    }
  }
  async function act(fn: () => Promise<unknown>) {
    busy = true;
    error = "";
    try {
      await fn();
      await load();
      await onrefresh();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }
  async function enqueue() {
    await act(async () => {
      attempt ||= crypto.randomUUID().replaceAll("-", "");
      await api(base(), "POST", {
        id: attempt,
        task_ids: selected,
        expected_versions: selectionVersions,
        profile_id: profileId,
      });
      selected = [];
      attempt = "";
    });
  }
  async function review(batch: Batch) {
    if (!batch.task) return;
    dialog?.close();
    await onselect(batch.task);
  }
  const publishBatch = (batch: Batch) =>
    act(() =>
      api(`${base()}/${batch.id}/publish`, "POST", {
        expected_version: batch.task?.version,
        revision: batch.task?.snapshot?.value.revision,
      }),
    );
  const rebuildBatch = (batch: Batch) =>
    act(() =>
      api(`${base()}/${batch.id}/rebuild`, "POST", {
        id: crypto.randomUUID().replaceAll("-", ""),
      }),
    );
  onMount(() => {
    const interval = setInterval(() => {
      if (dialog?.open && !busy)
        void load().catch((e) => {
          error = e.message;
        });
    }, 5000);
    return () => clearInterval(interval);
  });
</script>

<dialog
  bind:this={dialog}
  class="integration-dialog"
  aria-labelledby="integration-title"
>
  <header>
    <div>
      <span class="eyebrow">{project.name}</span>
      <h2 id="integration-title"><GitMerge size={22} /> Integration queue</h2>
    </div>
    <button
      class="icon-button"
      aria-label="Close integration queue"
      onclick={() => dialog?.close()}><X size={20} /></button
    >
  </header>
  {#if nextAction}
    <div class="next-step" role="status">
      <div>
        <span class="eyebrow">Your next step</span><strong
          >{label(nextAction)}</strong
        ><small
          >{nextAction.target_branch} · {nextAction.task?.snapshot?.value.revision?.slice(
            0,
            8,
          ) || nextAction.id.slice(0, 8)}</small
        >
      </div>
      {#if canManage && nextAction.status === "stale"}
        <button
          class="primary"
          disabled={busy || !nextAction.task?.can_message}
          onclick={() => rebuildBatch(nextAction!)}
          >Rebuild on latest target</button
        >
      {:else if canManage && nextAction.task?.snapshot?.value.status === "accepted" && nextAction.task.snapshot.value.verification === "verified"}
        <button
          class="primary"
          disabled={busy || !nextAction.task.live || !project.remote_url}
          onclick={() => publishBatch(nextAction!)}
          >{nextAction.status === "publishing"
            ? "Confirm publication"
            : "Open pull request"}<ArrowUpRight size={14} /></button
        >
      {:else if nextAction.task}<button
          class="primary"
          disabled={busy}
          onclick={() => review(nextAction!)}
          >Open review<ArrowUpRight size={14} /></button
        >{:else}<button
          disabled={busy}
          onclick={() =>
            act(() => api(`${base()}/${nextAction!.id}/prepare`, "POST"))}
          >Retry setup</button
        >{/if}
    </div>
  {/if}
  <div class="queue-body">
    {#if error}<p class="error" role="alert">{error}</p>{/if}
    <p class="intro">
      Combine accepted work, test it together, then review the final code before
      opening a pull request.
    </p>
    {#if !project.remote_url}<p class="notice">
        Checks and previews are available here. GitHub publication requires a
        repository connected through GitHub.
      </p>{/if}
    {#if canManage}
      <section class="sources" aria-label="Accepted tasks">
        <h3><Layers size={16} /> Start an integration</h3>
        {#if eligible.length}
          {#each eligible as task}
            <label class="source"
              ><input
                type="checkbox"
                value={task.id}
                bind:group={selected}
                disabled={busy}
                onchange={() => {
                  attempt = "";
                  selectionVersions[task.id] = task.version;
                }}
              /><span
                ><strong>{task.title}</strong><small
                  >{task.owner?.name ||
                    task.created_by?.name ||
                    "Project contributor"} · {task.snapshot?.value.revision?.slice(
                    0,
                    8,
                  )}</small
                ></span
              ></label
            >
          {/each}
          <div class="new-batch">
            <label
              >Agent profile<select
                bind:value={profileId}
                onchange={() => {
                  attempt = "";
                }}
                disabled={busy}
                >{#each profiles.filter((p) => p.available !== false) as p}<option
                    value={p.id}>{p.label}</option
                  >{/each}</select
              ></label
            >
          </div>
        {:else}<p>
            Accept a task’s changes to add them to an integration.
          </p>{/if}
      </section>
    {/if}
    <section class="batches" aria-label="Integration batches">
      <h3>Project queue <span>{batches.length}</span></h3>
      {#if !batches.length}<p>
          No integrations yet. Each batch gets its own worktree, combined
          checks, and app preview.
        </p>{/if}
      {#each [...batches].reverse() as batch (batch.id)}
        <article
          class:attention={batch.task?.snapshot?.value.status === "review" ||
            batch.task?.snapshot?.value.status === "accepted" ||
            batch.status === "stale"}
        >
          <div class="batch-heading">
            <strong
              >{batch.tasks.length}
              {batch.tasks.length === 1 ? "task" : "tasks"} · {batch.id.slice(
                0,
                8,
              )}</strong
            ><span class="status">{label(batch)}</span>
          </div>
          <p>
            {batch.tasks
              .map(
                (t) =>
                  tasks.find((source) => source.id === t.task_id)?.title ||
                  t.task_id.slice(0, 8),
              )
              .join(" · ")}
          </p>
          {#if batch.target_commit}<small
              >Target: {batch.target_branch} · {batch.target_commit.slice(
                0,
                8,
              )}</small
            >{/if}
          {#if batch.conflicts.length}<p class="notice">
              {batch.conflicts.length} overlapping {batch.conflicts.length === 1
                ? "file needs"
                : "files need"} a repair. Review the agent’s combined diff before
              accepting.
            </p>{/if}
          {#if batch.error}<p class="error">{batch.error}</p>{/if}
          {#if batch.publication}<small
              >Approved by {batch.publication.approved_by.name} · code {batch.publication.revision.slice(
                0,
                8,
              )}</small
            >{/if}
          <div class="actions">
            {#if batch.task}<button
                disabled={busy}
                onclick={() => review(batch)}
                >{batch.task.snapshot?.value.status === "review"
                  ? "Review integration"
                  : "Open integration task"}<ArrowUpRight size={14} /></button
              >{/if}
            {#if batch.pull_request}<a
                class="button primary"
                href={batch.pull_request.url}
                target="_blank"
                rel="noreferrer"
                >Pull request #{batch.pull_request.number}<ArrowUpRight
                  size={14}
                /></a
              ><button
                disabled={busy}
                onclick={() =>
                  act(async () => {
                    ci[batch.id] = await api<CI>(`${base()}/${batch.id}/ci`);
                  })}>Check GitHub CI</button
              >
            {:else if canManage && batch.task?.snapshot?.value.status === "accepted" && batch.task.snapshot.value.verification === "verified" && batch.status !== "stale" && !closed(batch)}
              <button
                class="primary"
                disabled={busy || !batch.task.live || !project.remote_url}
                onclick={() =>
                  act(() =>
                    api(`${base()}/${batch.id}/publish`, "POST", {
                      expected_version: batch.task?.version,
                      revision: batch.task?.snapshot?.value.revision,
                    }),
                  )}
                ><GitMerge size={14} />{batch.status === "publishing"
                  ? "Confirm publication"
                  : "Open pull request"}</button
              >
            {/if}
            {#if canManage && batch.error && ["queued", "preparing"].includes(batch.status)}<button
                disabled={busy}
                onclick={() =>
                  act(() => api(`${base()}/${batch.id}/prepare`, "POST"))}
                ><RefreshCw size={14} />Retry setup</button
              >{/if}
            {#if canManage && batch.status === "stale"}<button
                class="primary"
                disabled={busy || !batch.task?.can_message}
                onclick={() =>
                  act(() =>
                    api(`${base()}/${batch.id}/rebuild`, "POST", {
                      id: crypto.randomUUID().replaceAll("-", ""),
                    }),
                  )}>Rebuild on latest target</button
              >{/if}
            {#if canManage && !closed(batch) && !batch.publication}<button
                disabled={busy || (!!batch.task && !batch.task.can_message)}
                onclick={() =>
                  act(() => api(`${base()}/${batch.id}/cancel`, "POST"))}
                >Cancel batch</button
              >{/if}
          </div>
          {#if ci[batch.id]}{@const feedback = ci[batch.id]}
            <div class="ci">
              <strong>GitHub CI · {feedback.state}</strong
              >{#if !feedback.head_current}<p class="error">
                  The pull request now contains a different revision. These
                  checks do not verify its current code.
                </p>{/if}{#if !feedback.target_current}<p class="notice">
                  The target branch has moved since integration testing.
                  Revalidate on GitHub before merging.
                </p>{/if}{#if feedback.truncated}<p>
                  Showing the first 100 results. View the complete checks on
                  GitHub.
                </p>{/if}{#each feedback.checks as check}<p>
                  {check.name} <span>{check.conclusion || check.status}</span>
                </p>{/each}{#if !feedback.checks.length}<p>
                  No check runs reported yet.
                </p>{/if}
            </div>{/if}
        </article>
      {/each}
    </section>
  </div>
  <footer>
    <p>
      {selected.length
        ? `${selected.length} selected. The agent will install dependencies, repair conflicts, and run checks in E2B.`
        : "Final acceptance is required before publication."}
    </p>
    {#if canManage}<button
        class="primary"
        disabled={busy || !selected.length || !profileId}
        onclick={enqueue}
        ><Check size={15} />{busy ? "Working…" : "Queue & test"}</button
      >{/if}
  </footer>
</dialog>

<style>
  .integration-dialog {
    width: min(980px, calc(100vw - 32px));
    max-height: calc(100dvh - 40px);
    padding: 0;
    border: 1px solid var(--border-strong);
    border-radius: 16px;
    background: var(--surface-1);
    color: var(--text-1);
  }
  .integration-dialog[open] {
    display: flex;
    flex-direction: column;
  }
  .integration-dialog::backdrop {
    background: var(--overlay-scrim);
    backdrop-filter: blur(6px);
  }
  header,
  footer {
    padding: 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    flex-shrink: 0;
  }
  header {
    border-bottom: 1px solid var(--border);
  }
  .next-step {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: center;
    flex-shrink: 0;
    padding: 16px 24px;
    background: var(--warning-bg);
    border-bottom: 1px solid var(--warning-border);
  }
  .next-step strong,
  .next-step small {
    display: block;
    margin-top: 4px;
  }
  h2,
  h3 {
    display: flex;
    gap: 10px;
    align-items: center;
    margin: 6px 0;
  }
  h2 {
    font-size: 21px;
  }
  h3 {
    font-size: 14px;
    margin-bottom: 16px;
  }
  .eyebrow,
  small {
    color: var(--text-3);
    font-size: 12px;
  }
  .queue-body {
    overflow: auto;
    padding: 0 24px 24px;
    min-height: 0;
  }
  p {
    color: var(--text-2);
    line-height: 1.6;
    overflow-wrap: anywhere;
  }
  .intro {
    margin: 24px 0;
  }
  .sources {
    background: var(--surface-0);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 28px;
  }
  .source {
    display: flex;
    gap: 12px;
    padding: 12px 0;
    align-items: center;
    border-bottom: 1px solid var(--border);
  }
  .source input {
    accent-color: var(--accent);
    width: 16px;
    height: 16px;
    flex-shrink: 0;
  }
  .source strong {
    display: block;
    font-size: 13px;
    margin-bottom: 4px;
  }
  .new-batch {
    display: flex;
    margin-top: 18px;
  }
  .new-batch label {
    display: flex;
    gap: 12px;
    align-items: center;
    font-size: 12px;
    color: var(--text-3);
  }
  article {
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 14px;
  }
  article.attention {
    border-color: var(--warning-border);
  }
  .batch-heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .status {
    color: var(--accent-soft);
    font-size: 12px;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 18px;
  }
  .notice {
    color: var(--warning);
    font-size: 12px;
  }
  .error {
    color: var(--error);
    padding: 12px;
    background: var(--error-bg);
    border-radius: 8px;
  }
  .ci {
    margin-top: 18px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    font-size: 12px;
  }
  .ci p {
    display: flex;
    justify-content: space-between;
    gap: 16px;
  }
  footer {
    background: var(--surface-0);
    border-top: 1px solid var(--border);
  }
  footer p {
    margin: 0;
    font-size: 12px;
    max-width: 580px;
  }
  footer button {
    white-space: nowrap;
  }
  @media (max-width: 600px) {
    header,
    footer {
      padding: 16px;
    }
    .queue-body {
      padding: 0 16px 16px;
    }
    footer {
      flex-wrap: wrap;
    }
    footer button {
      width: 100%;
    }
  }
</style>
