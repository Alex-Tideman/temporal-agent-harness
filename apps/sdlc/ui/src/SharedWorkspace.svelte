<script lang="ts">
  import {
    GitBranch,
    Users,
    ArrowRightLeft,
    AlertTriangle,
    X,
    Send,
    RotateCcw,
  } from "@lucide/svelte";
  import { untrack } from "svelte";
  import { api, type Task, type Person } from "./types";

  let {
    task,
    onrefresh,
    onopen,
  }: {
    task: Task;
    onrefresh: () => Promise<void>;
    onopen: (id: string) => void;
  } = $props();
  type Peer = {
    id: string;
    title: string;
    owner?: Person;
    status: string;
    overlap: string[];
    changed_files: string[];
  };
  type Note = {
    id: string;
    source_id: string;
    target_id: string;
    author: Person;
    message: string;
    created_at: number;
  };
  type Board = { tasks: Peer[]; notes: Note[] };
  type Sandbox = {
    status: string;
    sandbox_id: string;
    can_recover: boolean;
    tasks: { id: string; checkpoint_at?: number }[];
  };
  let board = $state<Board>({ tasks: [], notes: [] });
  let members = $state<(Person & { role: string })[]>([]);
  let sandbox = $state<Sandbox>();
  let dialog = $state<HTMLDialogElement>();
  let busy = $state(false);
  let error = $state("");
  let notice = $state("");
  let recipient = $state("");
  let nextOwner = $state("");
  let message = $state("");
  let noteId = $state(crypto.randomUUID().replaceAll("-", ""));
  let confirmingRecovery = $state(false);
  let view = $state("coordination");
  let overlaps = $derived(board.tasks.filter((t) => t.overlap.length));
  let recovering = $derived(
    !!task.sandbox_status && task.sandbox_status !== "ready",
  );
  let idle = $derived(
    task.dispatch === "preparing"
      ? !task.preparation_running
      : !!task.live &&
          !!task.can_message &&
          ["review", "accepted", "failed", "cancelled"].includes(
            task.snapshot?.value.status ?? "",
          ),
  );
  const date = (value?: number) =>
    value ? new Date(value * 1000).toLocaleString() : "No saved checkpoint yet";

  $effect(() => {
    const id = task.id;
    let stopped = false;
    let loading = false;
    async function poll() {
      if (loading) return;
      loading = true;
      try {
        const result = await api<Board>(`/tasks/${id}/coordination`);
        if (!stopped) board = result;
      } catch {
        /* Mutations and dialog reads surface errors below. */
      } finally {
        loading = false;
      }
    }
    untrack(poll);
    const interval = setInterval(poll, 5000);
    return () => {
      stopped = true;
      clearInterval(interval);
    };
  });

  async function act(operation: () => Promise<void>) {
    if (busy) return;
    busy = true;
    error = "";
    notice = "";
    try {
      await operation();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }
  async function load() {
    const [people, environment, related] = await Promise.all([
      api<{ members: (Person & { role: string })[] }>(
        `/projects/${task.project_id}/members`,
      ),
      api<Sandbox>(`/projects/${task.project_id}/sandbox`),
      api<Board>(`/tasks/${task.id}/coordination`),
    ]);
    members = people.members.filter((p) => p.role !== "viewer");
    sandbox = environment;
    board = related;
    if (!recipient) recipient = board.tasks[0]?.id ?? "";
  }
  async function show(section = "coordination") {
    view = section;
    confirmingRecovery = false;
    nextOwner = "";
    dialog?.showModal();
    await act(load);
  }
  async function handoff() {
    await act(async () => {
      await api(`/tasks/${task.id}/handoff`, "POST", {
        user_id: nextOwner,
        expected_ownership_version: task.ownership_version,
      });
      nextOwner = "";
      await onrefresh();
      await load();
      notice =
        "Task handed off. The new owner can send the next message when ready.";
    });
  }
  async function send() {
    await act(async () => {
      await api(`/tasks/${task.id}/coordination`, "POST", {
        id: noteId,
        target_id: recipient,
        message,
      });
      message = "";
      noteId = crypto.randomUUID().replaceAll("-", "");
      await load();
      notice =
        "Note saved. The other agent sees it when it reads project coordination.";
    });
  }
  async function recover() {
    await act(async () => {
      await api(`/projects/${task.project_id}/sandbox/recover`, "POST");
      confirmingRecovery = false;
      await onrefresh();
      await load();
      notice =
        "Project worktrees restored. Send a follow-up to reinstall dependencies, run checks, and restart previews.";
    });
  }
</script>

<section class="shared-strip" aria-label="Shared workspace">
  <span class="owner"
    ><Users size={15} /><strong>{task.owner?.name ?? "Unassigned"}</strong><span
      >owns this task</span
    ></span
  >
  <span class="isolation"
    ><GitBranch size={14} />Isolated worktree · port {task.preview_port ??
      "pending"}</span
  >
  <button
    class:attention={recovering || overlaps.length > 0}
    onclick={() => show(recovering ? "sandbox" : "coordination")}
  >
    {#if recovering}<AlertTriangle size={14} />Sandbox needs attention
    {:else if overlaps.length}<AlertTriangle size={14} />{overlaps.length} overlapping
      {overlaps.length === 1 ? "task" : "tasks"}
    {:else}<Users size={14} />Coordinate{/if}
  </button>
  {#if task.can_handoff}<button
      disabled={!idle || recovering}
      onclick={() => show("ownership")}
      title={idle
        ? "Transfer this task to a teammate"
        : "Finish or stop the task before handing it off"}
      ><ArrowRightLeft size={14} />Hand off</button
    >{/if}
</section>

<dialog
  bind:this={dialog}
  class="shared-dialog"
  aria-labelledby="shared-title"
  oncancel={(e) => {
    if (busy) e.preventDefault();
  }}
>
  <header>
    <div>
      <span class="eyebrow">{task.project_name}</span>
      <h2 id="shared-title">Work together</h2>
    </div>
    <button
      class="icon-button"
      aria-label="Close coordination"
      disabled={busy}
      onclick={() => dialog?.close()}><X size={20} /></button
    >
  </header>
  <nav class="dialog-nav" aria-label="Workspace collaboration">
    {#each [["coordination", "Coordination"], ["ownership", "Ownership"], ["sandbox", "Sandbox"]] as [id, label]}
      <button
        class:selected={view === id}
        aria-pressed={view === id}
        disabled={busy}
        onclick={() => {
          view = id;
          error = "";
          notice = "";
        }}>{label}</button
      >
    {/each}
  </nav>
  <div class="dialog-content">
    {#if error}<p class="feedback error" role="alert">{error}</p>{/if}
    {#if notice}<p class="feedback" role="status">{notice}</p>{/if}
    {#if view === "ownership"}<section class="ownership">
        <div>
          <h3>{task.owner?.name ?? "Unassigned"} owns this task</h3>
          <p>
            One developer directs each task. Teammates can review changes and
            leave notes.
          </p>
        </div>
        {#if task.can_handoff}<div class="handoff-fields">
            <label
              >Hand off to<select
                bind:value={nextOwner}
                disabled={busy || !idle || recovering}
                ><option value="">Choose a teammate</option
                >{#each members.filter((p) => p.id !== task.owner?.id) as person}<option
                    value={person.id}>{person.name}</option
                  >{/each}</select
              ></label
            ><button
              disabled={busy || !idle || recovering || !nextOwner}
              onclick={handoff}><ArrowRightLeft size={14} />Hand off</button
            >
          </div>
          <p>
            {idle
              ? "The code is checkpointed and this task’s preview stops before ownership changes. No agent starts automatically."
              : "Finish or stop the task before handing it off. A pause still has an active agent turn."}
          </p>{/if}
      </section>{:else if view === "coordination"}
      <section>
        <h3>Other work in this project</h3>
        <p>
          Potential overlaps reflect saved changes and planned scope. Each task
          keeps its own code and preview.
        </p>
        <ul class="peers">
          {#each board.tasks as peer}<li>
              <button
                class="peer-link"
                onclick={() => {
                  dialog?.close();
                  onopen(peer.id);
                }}
                ><strong>{peer.title}</strong><small
                  >{peer.owner?.name ?? "Unassigned"} · {peer.status.replaceAll(
                    "_",
                    " ",
                  )}</small
                ></button
              >{#if peer.overlap.length}<span class="overlap"
                  ><AlertTriangle size={13} />{peer.overlap.join(", ")}</span
                >{/if}
            </li>{:else}<li class="muted">No other tasks yet.</li>{/each}
        </ul>
      </section>
      <section>
        <h3>Coordination notes</h3>
        <p>
          Share dependencies and agree on overlapping edits. Notes do not
          interrupt another agent or approve changes.
        </p>
        {#if task.can_review && board.tasks.length}<form
            onsubmit={(e) => {
              e.preventDefault();
              send();
            }}
          >
            <label
              >To task<select bind:value={recipient} disabled={busy}
                >{#each board.tasks as peer}<option value={peer.id}
                    >{peer.title}</option
                  >{/each}</select
              ></label
            ><label
              >Note<textarea
                bind:value={message}
                rows="3"
                maxlength="4000"
                disabled={busy}
                placeholder="I’m updating the API response. Which fields does your UI need?"
                oninput={() => {
                  noteId = crypto.randomUUID().replaceAll("-", "");
                }}></textarea></label
            ><button
              class="primary"
              disabled={busy || !recipient || !message.trim()}
              ><Send size={14} />Send note</button
            >
          </form>{/if}
        <ol class="notes">
          {#each board.notes as note}<li>
              <div>
                <strong>{note.author.name}</strong><small
                  >{date(note.created_at)} · {note.target_id === task.id
                    ? "Incoming"
                    : "Outgoing"}</small
                >
              </div>
              <p>{note.message}</p>
            </li>{:else}<li class="muted">
              No notes for this task yet.
            </li>{/each}
        </ol>
      </section>
    {:else}<section class="recovery">
        <h3>Project sandbox</h3>
        <p>
          <span class="tiny-pill"
            >{sandbox?.status.replaceAll("_", " ") ?? "Loading"}</span
          > Shared environment · separate task branches and previews
        </p>
        <p>
          This task’s last code checkpoint: <strong
            >{date(task.checkpoint?.saved_at)}</strong
          >
        </p>
        {#if sandbox?.can_recover}
          {#if confirmingRecovery}<div class="recovery-confirm" role="alert">
              <h4>Replace the sandbox for every task?</h4>
              <p>
                Finish or stop every task first. Reachable code is checkpointed
                before replacement. If the sandbox is gone, recovery uses the
                last saved checkpoints. Later unsaved changes cannot be
                recovered.
              </p>
              <p>
                Previews stop. Dependencies need reinstalling, and restored
                changes need fresh verification. Secrets, ignored files, running
                processes, and task Git history beyond the starting commit are
                not restored.
              </p>
              <div class="actions">
                <button
                  disabled={busy}
                  onclick={() => (confirmingRecovery = false)}
                  >Keep current sandbox</button
                ><button class="primary" disabled={busy} onclick={recover}
                  >{busy
                    ? "Recovering…"
                    : "Replace and restore all worktrees"}</button
                >
              </div>
            </div>
          {:else}<button
              disabled={busy}
              onclick={() => (confirmingRecovery = true)}
              ><RotateCcw size={14} />{recovering
                ? "Continue sandbox recovery"
                : "Recover sandbox"}</button
            >{/if}
        {:else}<p>A project maintainer can recover the shared sandbox.</p>{/if}
      </section>{/if}
  </div>
</dialog>

<style>
  .shared-strip {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 12px 16px;
    border: 1px solid var(--border);
    border-radius: 9px;
    margin-bottom: 16px;
    font-size: 12px;
    background: var(--surface-1);
  }
  .owner,
  .isolation {
    display: flex;
    align-items: center;
    gap: 7px;
  }
  .owner span,
  .isolation,
  .muted {
    color: var(--text-3);
  }
  .isolation {
    margin-right: auto;
  }
  .shared-strip button {
    font-size: 12px;
    padding: 7px 10px;
  }
  .attention,
  .overlap {
    color: var(--warning, #e5b769);
  }
  dialog {
    width: min(780px, calc(100vw - 32px));
    max-height: calc(100dvh - 48px);
    padding: 0;
    border: 1px solid var(--border);
    border-radius: 14px;
    background: var(--surface-1);
    color: var(--text-1);
  }
  dialog::backdrop {
    background: rgb(0 0 0 / 65%);
    backdrop-filter: blur(5px);
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 22px 26px;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--surface-1);
    z-index: 1;
  }
  h2 {
    font-size: 20px;
    margin: 6px 0 0;
  }
  h3 {
    font-size: 14px;
    margin: 0 0 7px;
  }
  h4 {
    font-size: 14px;
    margin: 0;
  }
  .dialog-nav {
    display: flex;
    gap: 4px;
    padding: 10px 26px;
    border-bottom: 1px solid var(--border);
  }
  .dialog-nav button {
    font-size: 12px;
    border-color: transparent;
    background: transparent;
  }
  .dialog-nav button.selected {
    background: var(--surface-2);
    color: var(--text-1);
    border-color: var(--border);
  }
  .peers {
    max-height: 150px;
    overflow-y: auto;
  }
  .notes {
    max-height: 160px;
    overflow-y: auto;
  }
  .dialog-content {
    padding: 0 26px 10px;
  }
  .dialog-content section {
    padding: 24px 0;
    border-bottom: 1px solid var(--border);
  }
  .dialog-content section:last-child {
    border-bottom: 0;
  }
  p {
    color: var(--text-3);
    font-size: 12px;
    line-height: 1.65;
    margin: 6px 0 12px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 8px;
    font-size: 12px;
    color: var(--text-2);
    min-width: 0;
  }
  select,
  textarea {
    width: 100%;
    font: inherit;
  }
  textarea {
    resize: vertical;
  }
  .handoff-fields {
    display: flex;
    align-items: end;
    gap: 12px;
    margin-top: 16px;
  }
  .handoff-fields label {
    flex: 1;
  }
  ul,
  ol {
    list-style: none;
    padding: 0;
    margin: 16px 0 0;
  }
  li {
    padding: 12px 0;
    border-top: 1px solid var(--border);
    font-size: 12px;
  }
  .peer-link {
    display: flex;
    flex-direction: column;
    align-items: start;
    text-align: left;
    gap: 6px;
    background: none;
    border: 0;
    padding: 0;
    width: 100%;
  }
  .peer-link strong {
    font-weight: 500;
    white-space: normal;
    overflow-wrap: anywhere;
  }
  small {
    font-size: 11px;
    color: var(--text-3);
  }
  .overlap {
    display: flex;
    align-items: start;
    gap: 6px;
    font: 11px var(--font-mono);
    margin-top: 10px;
    overflow-wrap: anywhere;
  }
  form {
    display: grid;
    gap: 12px;
    margin-top: 18px;
  }
  form button {
    justify-self: end;
  }
  .notes li div {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }
  .notes p {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-2);
  }
  .feedback,
  .recovery-confirm {
    padding: 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .feedback {
    margin-top: 18px;
    color: var(--text-1);
  }
  .error {
    color: var(--warning, #e5b769);
  }
  .actions {
    display: flex;
    justify-content: end;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 16px;
  }
  @media (max-width: 600px) {
    header {
      padding: 18px;
    }
    .dialog-content {
      padding: 0 18px;
    }
    .isolation {
      display: none;
    }
    .owner {
      width: 100%;
    }
    .handoff-fields {
      align-items: stretch;
      flex-direction: column;
    }
    .actions button {
      width: 100%;
    }
  }
</style>
