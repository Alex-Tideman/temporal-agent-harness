<script lang="ts">
  import {
    AlertCircle,
    ArrowRight,
    Check,
    GitMerge,
    Play,
    X,
  } from "@lucide/svelte";
  import { taskAttention } from "./attention";
  import type { Task } from "./types";

  let {
    task,
    busy,
    error,
    oncontrol,
    onreview,
    oncontinue,
    onsettings,
    onmerge,
    onretry,
  }: {
    task: Task;
    busy: boolean;
    error: string;
    oncontrol: (
      command: string,
      approved?: boolean,
      text?: string,
      gateId?: string,
      expectedVersion?: number,
    ) => Promise<boolean>;
    onreview: () => void;
    oncontinue: () => void;
    onsettings: () => void;
    onmerge: () => void;
    onretry: () => Promise<void>;
  } = $props();
  let progress = $derived(task.snapshot?.value);
  let gate = $derived(progress?.gate);
  let mergeReady = $derived(
    task.engine === "v2" &&
      progress?.status === "accepted" &&
      progress.mode === "change" &&
      task.workspace &&
      !(
        progress.project_merge &&
        progress.project_merge.revision === progress.revision
      ),
  );
  let label = $derived(
    taskAttention(task) || (mergeReady ? "Merge accepted changes" : ""),
  );
  let needsNote = $derived(
    progress?.engine === "v2" &&
      progress.status === "review" &&
      progress.mode === "change" &&
      progress.verification !== "verified",
  );
  let disabled = $derived(busy || !task.live || task.can_control === false);
  let dialog = $state<HTMLDialogElement>();
  let feedback = $state("");
  let identity = $derived(`${task.id}:${gate?.id ?? progress?.status}`);
  $effect(() => {
    identity;
    feedback = "";
    dialog?.close();
  });
  let description = $derived(
    gate
      ? gate.kind === "plan"
        ? "Review the proposed scope. Approval lets the agent begin the work."
        : gate.kind === "command"
          ? `The agent is waiting to run checks ${task.workspace_backend === "e2b" ? "in your E2B sandbox" : "on your computer"}.`
          : "The agent needs your answer before it can continue."
      : task.preparation_error
        ? "Workspace setup stopped before the agent started. Retry setup to continue this task."
        : task.message_error
          ? "Your follow-up was not accepted. Review it before sending again."
          : progress?.status === "failed"
            ? "Your changes are saved. Review the error, then prepare a follow-up to continue."
            : progress?.status === "paused"
              ? "Work is paused. Resume when you are ready for the agent to continue."
              : progress?.status === "review"
                ? progress.mode === "ask"
                  ? "The answer is ready. Read it, then accept or send a follow-up."
                  : needsNote
                    ? "Verification is incomplete. Review the changes and add a note if you accept them."
                    : progress.verification === "verified"
                      ? "Checks passed. Review the changes, then accept or request a follow-up."
                      : "The work is ready. Review the changes, then accept or request a follow-up."
                : "Your changes are accepted. Merge them when you are ready to update your project.",
  );
  let decisionVersion = $state<number>();
  const details = () => {
    decisionVersion = task.version;
    dialog?.showModal();
  };
  async function retrySetup() {
    dialog?.close();
    await onretry();
  }
  async function respond(approved: boolean) {
    if (disabled || !gate || (gate.kind === "question" && !feedback.trim()))
      return;
    if (await oncontrol("respond", approved, feedback, gate.id))
      dialog?.close();
  }
  async function accept() {
    if (busy || !task.live || task.can_accept === false || !task.can_message)
      return;
    if (needsNote && !feedback.trim()) {
      details();
      return;
    }
    if (await oncontrol("accept", true, feedback, "", decisionVersion))
      dialog?.close();
  }
</script>

{#if label}
  <section class="action-bar" aria-label="Action needed">
    <div
      class="action-summary"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <span class="attention-icon"><AlertCircle size={21} /></span>
      <div>
        <span class="eyebrow"
          >{mergeReady
            ? "Ready for your next step"
            : "Your action needed"}</span
        >
        <h2>{label}</h2>
        <p class="action-task" title={task.title}>{task.title}</p>
        <p>{description}</p>
      </div>
    </div>
    <div class="action-buttons">
      {#if gate}
        <button onclick={details}
          >{gate.kind === "question"
            ? "Read question"
            : "Review details"}</button
        >
        <button class="primary" {disabled} onclick={details}
          >{gate.kind === "question" ? "Answer question" : label}<ArrowRight
            size={15}
          /></button
        >
      {:else if task.preparation_error}
        <button onclick={details}>View error</button><button
          class="primary"
          disabled={busy ||
            task.can_control === false ||
            task.preparation_running}
          onclick={retrySetup}>Retry setup<ArrowRight size={15} /></button
        >
      {:else if task.message_error || progress?.status === "failed"}
        <button onclick={details}>View error</button><button
          class="primary"
          disabled={busy || task.can_control === false || !task.can_message}
          onclick={oncontinue}>Prepare follow-up<ArrowRight size={15} /></button
        >
      {:else if progress?.status === "paused"}
        <button class="primary" {disabled} onclick={() => oncontrol("resume")}
          ><Play size={15} />Resume task</button
        >
      {:else if progress?.status === "review"}
        <button onclick={progress.mode === "ask" ? details : onreview}
          >{progress.mode === "ask" ? "Read answer" : "Review changes"}</button
        >
        <button
          class="primary"
          disabled={busy ||
            !task.live ||
            task.can_accept === false ||
            !task.can_message}
          onclick={details}
          ><Check size={15} />{progress.mode === "ask"
            ? "Accept answer"
            : "Accept changes"}</button
        >
      {:else if mergeReady}
        <button onclick={onreview}>Review changes</button><button
          class="primary"
          disabled={busy || !task.live || task.can_merge === false}
          onclick={onmerge}><GitMerge size={15} />Merge into project</button
        >
      {/if}
    </div>
    {#if !task.live && !task.preparation_error}<p class="action-notice">
        Reconnecting to the worker. Actions will become available when the task
        is live.
      </p>{/if}
    {#if task.can_control === false}<p class="action-notice">
        {task.can_review === false
          ? "You have read-only access to this task."
          : task.workspace_layout === "project-worktree"
            ? task.sandbox_status && task.sandbox_status !== "ready"
              ? "The project sandbox needs recovery. Open Sandbox needs attention above."
              : `${task.owner?.name ?? "Another developer"} owns this task. You can review it and coordinate changes; a handoff transfers control.`
            : "You can review this legacy local task. Only the workspace administrator can operate it; start an E2B task for shared development."}
      </p>{:else if task.can_accept === false && (progress?.status === "review" || mergeReady)}<p
        class="action-notice"
      >
        A project maintainer or owner can accept and merge these changes. You
        can review files and leave comments.
      </p>{/if}
    {#if error}<p class="action-error" role="alert">{error}</p>{/if}
  </section>
{/if}

<dialog
  bind:this={dialog}
  class="action-dialog"
  aria-labelledby="decision-title"
>
  <header>
    <div>
      <span class="eyebrow">Your next step</span>
      <h2 id="decision-title">{gate?.title || label}</h2>
    </div>
    <button
      class="icon-button"
      aria-label="Close decision details"
      onclick={() => dialog?.close()}><X size={18} /></button
    >
  </header>
  <div class="decision-body">
    <p>{description}</p>
    {#if gate}
      <pre>{gate.detail}</pre>
      {#if gate.kind === "command"}
        <p class="execution-location">
          {#if task.workspace_backend === "e2b"}Runs in your E2B sandbox with
            network access. Your source checkout changes when you merge an
            accepted task.{:else}Runs in <code>{task.workspace}</code> on your computer,
            with access to host files and network. Provider keys are excluded from
            the child environment. Only approve commands you trust.{/if}
        </p>
      {/if}
      <label for="decision-feedback"
        >{gate.kind === "question"
          ? "Your answer (required)"
          : "Feedback or requested changes (optional)"}</label
      >
      <textarea
        id="decision-feedback"
        rows="3"
        bind:value={feedback}
        placeholder={gate.kind === "question"
          ? "Write your answer…"
          : "Describe what you would like changed…"}></textarea>
    {:else if task.preparation_error || task.message_error || progress?.status === "failed"}
      <h3>{progress?.failure?.title || "What happened"}</h3>
      <p>
        {task.preparation_error ||
          task.message_error?.error ||
          progress?.failure?.explanation ||
          progress?.focus}
      </p>
      {#if progress?.failure?.actions?.length}<ol>
          {#each progress.failure.actions as action}<li>{action}</li>{/each}
        </ol>{/if}
      {#if task.preparation_error}<p>
          Retry setup reuses the saved sandbox when available. Your repository
          and any existing workspace files are kept.
        </p>{:else}<p>
          Preparing a follow-up does not start the agent. Review the message and
          select Send message when ready.
        </p>{/if}
    {:else if progress?.status === "review"}
      {#if progress.mode === "ask"}<pre>{progress.outcome ||
            progress.entries.at(-1)?.text ||
            progress.focus}</pre>{/if}
      {#if progress.mode === "change"}
        {#if progress.missing_evidence?.length}<ul>
            {#each progress.missing_evidence as item}<li>{item}</li>{/each}
          </ul>{/if}
        <label for="acceptance-feedback"
          >Acceptance note {needsNote ? "(required)" : "(optional)"}</label
        >
        <textarea
          id="acceptance-feedback"
          rows="3"
          bind:value={feedback}
          placeholder="Describe any remaining checks or findings you are accepting."
        ></textarea>
      {/if}
    {/if}
  </div>
  <footer>
    {#if error}<p class="action-error" role="alert">{error}</p>{/if}
    {#if !task.live && !task.preparation_error}<p class="action-notice">
        Waiting for the worker to reconnect.
      </p>{/if}
    <div class="action-buttons">
      {#if gate}
        {#if gate.kind !== "question"}<button
            {disabled}
            onclick={() => respond(false)}>Decline / request changes</button
          >{/if}
        <button
          class="primary"
          disabled={disabled || (gate.kind === "question" && !feedback.trim())}
          onclick={() => respond(true)}
          >{gate.kind === "question" ? "Send answer" : label}<ArrowRight
            size={15}
          /></button
        >
      {:else if progress?.status === "review"}
        <button
          onclick={() => {
            dialog?.close();
            oncontinue();
          }}>Prepare follow-up</button
        >
        <button
          class="primary"
          disabled={busy ||
            !task.live ||
            task.can_accept === false ||
            !task.can_message ||
            (needsNote && !feedback.trim())}
          onclick={accept}
          ><Check size={15} />{progress.mode === "ask"
            ? "Accept answer"
            : "Accept changes"}</button
        >
      {:else}
        <button
          onclick={() => {
            dialog?.close();
            onsettings();
          }}>Open settings</button
        >
        {#if task.preparation_error}<button
            class="primary"
            disabled={busy ||
              task.can_control === false ||
              task.preparation_running}
            onclick={retrySetup}>Retry setup<ArrowRight size={15} /></button
          >{:else}<button
            class="primary"
            disabled={busy || task.can_control === false || !task.can_message}
            onclick={() => {
              dialog?.close();
              oncontinue();
            }}>Prepare follow-up<ArrowRight size={15} /></button
          >{/if}
      {/if}
    </div>
  </footer>
</dialog>

<style>
  .action-bar {
    position: sticky;
    top: var(--app-header-height);
    z-index: 20;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px 20px;
    padding: 16px 32px;
    background: color-mix(in srgb, var(--warning) 9%, var(--surface-0));
    border-bottom: 1px solid var(--warning-border);
    box-shadow: 0 4px 16px #0003;
  }
  .action-summary {
    display: flex;
    gap: 12px;
    align-items: center;
    flex: 1 1 280px;
    min-width: 0;
  }
  .action-summary > div {
    min-width: 0;
  }
  .action-task {
    color: var(--warning);
    margin-bottom: 3px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .attention-icon {
    color: var(--warning);
    display: flex;
    flex-shrink: 0;
    padding: 9px;
    border-radius: 10px;
    background: var(--warning-bg);
    border: 1px solid var(--warning-border);
  }
  .eyebrow {
    color: var(--warning);
    font-size: 11px;
  }
  h2 {
    margin: 4px 0;
    font-size: 16px;
    line-height: 1.3;
  }
  p {
    margin: 0;
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-2);
  }
  .action-buttons {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .action-buttons button {
    min-height: 40px;
  }
  .action-buttons .primary {
    background: var(--warning);
    border-color: var(--warning);
    color: var(--surface-0);
  }
  .action-notice,
  .action-error {
    flex-basis: 100%;
    font-size: 12px;
  }
  .action-notice {
    color: var(--warning);
  }
  .action-error {
    color: var(--error);
    overflow-wrap: anywhere;
    max-height: 80px;
    overflow-y: auto;
  }
  .action-dialog {
    width: min(740px, calc(100vw - 32px));
    max-height: calc(100dvh - 32px);
    padding: 0;
    border: 1px solid var(--warning-border);
    border-radius: 12px;
    background: var(--surface-1);
    color: var(--text-1);
    overflow: hidden;
  }
  .action-dialog[open] {
    display: flex;
    flex-direction: column;
  }
  .action-dialog::backdrop {
    background: var(--overlay-scrim);
    backdrop-filter: blur(5px);
  }
  header {
    padding: 20px 24px;
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: start;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  header h2 {
    overflow-wrap: anywhere;
  }
  .decision-body {
    overflow-y: auto;
    padding: 24px;
    min-height: 0;
    overscroll-behavior: contain;
  }
  .decision-body p,
  pre,
  ul,
  ol {
    margin: 0 0 20px;
    line-height: 1.7;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font: 12px/1.8 var(--font-mono);
    color: var(--text-2);
  }
  li {
    margin-bottom: 8px;
  }
  label {
    display: block;
    font-size: 12px;
    margin-bottom: 8px;
  }
  textarea {
    width: 100%;
  }
  .execution-location {
    padding: 12px;
    border: 1px solid var(--warning-border);
    border-radius: 6px;
    color: var(--warning);
  }
  footer {
    border-top: 1px solid var(--border);
    padding: 16px 24px;
    flex-shrink: 0;
  }
  footer p {
    margin-bottom: 12px;
  }
  footer .action-buttons {
    justify-content: flex-end;
  }
  @media (max-width: 760px) {
    .action-bar {
      padding: 12px 15px;
      gap: 10px;
    }
    .action-summary {
      flex-basis: 100%;
      gap: 8px;
    }
    .action-summary p {
      font-size: 11px;
    }
    .action-buttons {
      width: 100%;
    }
    .action-buttons button {
      flex: 1;
      min-height: 44px;
    }
    header,
    .decision-body,
    footer {
      padding: 16px;
    }
  }
</style>
