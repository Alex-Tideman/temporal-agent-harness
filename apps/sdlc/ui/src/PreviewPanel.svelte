<script lang="ts">
  import { onMount, untrack } from "svelte";
  import {
    Monitor,
    ExternalLink,
    Play,
    Square,
    RefreshCw,
    Globe,
    Terminal,
    Settings2,
    Check,
  } from "@lucide/svelte";
  import { api } from "./types";

  let {
    taskId,
    visible,
    canControl = true,
  }: { taskId: string; visible: boolean; canControl?: boolean } = $props();
  type Preview = {
    checkpoint_error?: string;
    supported: boolean;
    status: "idle" | "starting" | "running" | "stopped";
    command?: string;
    cwd?: string;
    port?: number;
    url: string;
    logs: string;
    exit_code?: number | null;
    launch_unconfirmed?: boolean;
    automatic?: boolean;
    detected?: boolean;
    framework?: string;
    phase?: string;
    detail?: string;
    http_status?: number | null;
    healthy?: boolean;
  };
  let data = $state<Preview | null>(null);
  let command = $state("");
  let cwd = $state(".");
  let port = $state<number | undefined>(3000);
  let custom = $state(false);
  let busy = $state(false);
  let loading = $state(false);
  let error = $state("");
  let frameVersion = $state(0);
  let initialized = false;
  let disposed = false;
  let active = $derived(
    data?.status === "starting" || data?.status === "running",
  );
  let endpoint = $derived(`/tasks/${taskId}/preview`);

  async function refresh(clearError = true) {
    if (busy || loading || disposed) return;
    loading = true;
    try {
      const result = await api<Preview>(endpoint);
      if (disposed) return;
      data = result;
      if (!initialized) {
        command = result.command ?? "";
        cwd = result.cwd ?? ".";
        port = result.port ?? 3000;
        custom = result.automatic === false;
        initialized = true;
      }
      if (clearError) error = "";
    } catch (e) {
      if (!disposed) error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  async function change(method: "POST" | "DELETE") {
    if (!canControl || busy || loading) return;
    busy = true;
    error = "";
    try {
      const result = await api<Preview>(
        endpoint,
        method,
        method === "POST" ? (custom ? { command, cwd, port } : {}) : undefined,
      );
      if (!disposed) data = result;
    } catch (e) {
      if (!disposed) error = (e as Error).message;
    } finally {
      busy = false;
    }
    if (error) await refresh(false);
  }

  $effect(() => {
    if (visible) untrack(() => void refresh());
  });
  onMount(() => {
    const timer = setInterval(() => {
      if (visible && active && !document.hidden) void refresh();
    }, 4000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  });
</script>

<section class="preview-panel" aria-label="App preview">
  <header class="preview-toolbar">
    <div class="preview-heading">
      <Monitor size={17} />
      <strong>App preview</strong>
      {#if data?.supported}
        <span
          class="preview-status"
          class:live={data.status === "running"}
          aria-live="polite"
        >
          <span class="status-dot"></span>
          {data.status === "running"
            ? "Live"
            : data.status === "starting"
              ? "Starting"
              : data.status === "stopped"
                ? data.phase === "failed"
                  ? "Needs repair"
                  : "Stopped"
                : "Not started"}
        </span>
      {/if}
    </div>
    <div class="preview-actions">
      {#if active || data?.launch_unconfirmed}
        <button
          disabled={!canControl || busy || loading}
          onclick={() => change("DELETE")}
          ><Square size={13} /> Stop preview</button
        >
      {/if}
      {#if data?.supported && !active && !data.launch_unconfirmed}
        <button
          class="primary"
          type="submit"
          form={"preview-setup-" + taskId}
          disabled={!canControl ||
            busy ||
            loading ||
            (custom && (!command.trim() || !port))}
          ><Play size={14} />{busy
            ? "Preparing…"
            : data.phase === "failed"
              ? "Retry preview"
              : "Start preview"}</button
        >
      {/if}
      {#if data?.status === "running" && data.url}
        <button
          title="Reload the app preview"
          aria-label="Reload the app preview"
          onclick={() => frameVersion++}><RefreshCw size={14} /></button
        >
        <a
          class="button"
          href={data.url}
          target="_blank"
          rel="noopener noreferrer"
          ><ExternalLink size={14} /> Open in new tab</a
        >
      {/if}
    </div>
  </header>

  {#if data?.checkpoint_error}<div class="preview-error" role="alert">
      {data.checkpoint_error}
    </div>{/if}
  {#if error}
    <div class="preview-error" role="alert">
      <span>{error}</span><button
        disabled={busy || loading}
        onclick={() => refresh()}>Retry status</button
      >
    </div>
  {/if}

  {#if data?.launch_unconfirmed && !active}
    <p class="preview-help">
      The last launch could not be confirmed. Select “Stop preview” before
      trying again.
    </p>
  {/if}

  {#if !data}
    <div class="preview-empty">
      <Monitor size={28} />
      <p>
        {loading ? "Checking the sandbox…" : "Preview status is unavailable."}
      </p>
    </div>
  {:else if !data.supported}
    <div class="preview-empty">
      <Monitor size={28} />
      <h3>An E2B workspace is required</h3>
      <p>Create a task with E2B enabled to run and view its web app here.</p>
    </div>
  {:else if active}
    <div class="preview-address">
      <Globe size={13} /><span>{data.url.replace(/^https?:\/\//, "")}</span
      ><span class="public-label">Public preview</span>
    </div>
    {#if data.status === "running" && visible}
      {#key frameVersion}
        <iframe
          title="Live app preview"
          src={data.url}
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
          referrerpolicy="no-referrer"
        ></iframe>
      {/key}
      <p class="preview-help">
        <Check size={13} /> HTTP {data.http_status ?? 200} check passed. Changes update
        automatically with live reload.
      </p>
    {:else if data.status === "starting"}
      <div class="preview-empty starting">
        <span class="starting-icon"><Monitor size={26} /></span>
        <h3>
          {data.phase === "tooling"
            ? "Preparing the sandbox"
            : data.phase === "installing"
              ? "Installing dependencies"
              : "Starting and checking your app"}
        </h3>
        <p>
          {data.detail ||
            "We’ll install what’s needed and check that the app responds."}<br
          />Your preview appears here when it’s ready.
        </p>
        <div class="setup-progress" aria-label="Preview setup progress">
          {#each ["Prepare tooling", "Install dependencies", "Start app", "Check response"] as step, index}
            <span
              class:complete={data.phase === "installing"
                ? index < 1
                : data.phase !== "tooling" && index < 2}
              ><span>{index + 1}</span>{step}</span
            >
          {/each}
        </div>
      </div>
    {/if}
  {:else}
    <form
      id={"preview-setup-" + taskId}
      class="preview-setup"
      onsubmit={(event) => {
        event.preventDefault();
        void change("POST");
      }}
    >
      <div class="setup-heading">
        <h3>
          {data.phase === "failed"
            ? "The app needs a startup fix"
            : "Your app, ready to preview"}
        </h3>
        <p>
          {data.phase === "failed"
            ? "The agent receives startup errors during verification and can repair them. Retry after a fix, or adjust the advanced settings."
            : "The agent handles tooling, dependencies, and startup, then checks that the app responds."}
        </p>
      </div>
      <div class="preview-disclosure">
        <Globe size={15} /><span
          >Runs in this task’s sandbox. Anyone with the preview link can access
          the app while it’s running.</span
        >
      </div>
      <details class="preview-advanced">
        <summary
          ><Settings2 size={14} />Advanced settings<span
            >{custom ? "Custom" : data.framework || "Automatic"}</span
          ></summary
        >
        <div class="advanced-fields">
          <label class="custom-toggle"
            ><input
              type="checkbox"
              bind:checked={custom}
              disabled={busy}
            />Override automatic setup</label
          >
          <div class="setup-fields">
            <label
              >App directory<input
                bind:value={cwd}
                placeholder=". or apps/web"
                maxlength="4000"
                disabled={busy || !custom}
              /></label
            >
            <label
              >Port<input
                type="number"
                bind:value={port}
                min="1024"
                max="65535"
                required={custom}
                disabled={busy || !custom}
              /></label
            >
          </div>
          <label
            >Start command<textarea
              bind:value={command}
              rows="2"
              placeholder="npm install && npm run dev -- --host 0.0.0.0 --port 3000"
              maxlength="4000"
              required={custom}
              disabled={busy || !custom}></textarea></label
          >
          <p class="preview-help">
            Bind your server to <code>0.0.0.0</code> on the port above.
            <code>HOST</code>
            and <code>PORT</code> are provided to the command. Custom commands replace
            automatic dependency setup.
          </p>
        </div>
      </details>
    </form>
  {/if}

  {#if data?.logs || data?.status === "starting" || data?.status === "stopped"}
    <details class="preview-logs" open={data.phase === "failed"}>
      <summary
        ><Terminal size={14} />Server logs{#if data.exit_code != null}<span
            >Exit {data.exit_code}</span
          >{/if}</summary
      >
      <pre>{data.logs || "Waiting for server output…"}</pre>
    </details>
  {/if}
  {#if data?.supported}
    <footer>
      Runs in this task’s E2B sandbox. Stop preview ends the server; your files
      stay in the workspace.
    </footer>
  {/if}
</section>

<style>
  .preview-panel {
    min-width: 0;
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    background: var(--surface-0);
  }
  .preview-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
    padding: 15px 20px;
    background: var(--surface-1);
    border-bottom: 1px solid var(--border);
  }
  .preview-heading,
  .preview-actions {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .preview-heading > :global(svg) {
    color: var(--text-3);
  }
  .preview-heading strong {
    font-size: 13px;
  }
  .preview-status {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--text-3);
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: 5px;
  }
  .preview-status.live {
    color: var(--success);
  }
  .status-dot {
    width: 5px;
    height: 5px;
    background: currentColor;
    border-radius: 50%;
  }
  .preview-error {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 14px 20px;
    color: var(--error);
    background: var(--error-bg);
    font-size: 13px;
  }
  .preview-error button {
    flex-shrink: 0;
  }
  .preview-empty {
    text-align: center;
    padding: 60px 24px;
    color: var(--text-3);
  }
  h3 {
    margin: 12px 0 8px;
    font-size: 16px;
    font-weight: 550;
    letter-spacing: -0.02em;
    color: var(--text-1);
  }
  p {
    line-height: 1.65;
    font-size: 13px;
    margin: 0;
  }
  .preview-address {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 10px 20px;
    font: 11px var(--font-mono);
    color: var(--text-3);
    border-bottom: 1px solid var(--border);
  }
  .preview-address > span:first-of-type {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .public-label {
    margin-left: auto;
    white-space: nowrap;
    font: 11px var(--font-sans);
  }
  iframe {
    display: block;
    border: 0;
    width: 100%;
    height: 65vh;
    min-height: 440px;
    background: white;
  }
  .preview-panel > .preview-help {
    padding: 12px 20px;
  }
  .preview-setup {
    padding: 26px;
    max-width: 900px;
    margin: auto;
  }
  .setup-heading {
    margin-bottom: 24px;
  }
  .setup-heading h3 {
    margin-top: 0;
  }
  .setup-heading p,
  .preview-help {
    color: var(--text-3);
  }
  .setup-fields {
    display: grid;
    grid-template-columns: 1fr 130px;
    gap: 16px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 9px;
    color: var(--text-2);
    font-size: 12px;
    margin-bottom: 18px;
  }
  input,
  textarea {
    width: 100%;
    min-width: 0;
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    background: var(--control-bg);
    padding: 10px 12px;
    color: var(--text-1);
    font: 12px/1.6 var(--font-mono);
  }
  textarea {
    resize: vertical;
  }
  input:focus,
  textarea:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: inset 0 0 0 1px var(--accent);
  }
  .preview-help {
    font-size: 12px;
  }
  code {
    font: 11px var(--font-mono);
    color: var(--text-2);
  }
  .preview-disclosure {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--text-3);
    font-size: 12px;
    line-height: 1.6;
    margin-bottom: 24px;
  }
  .preview-disclosure :global(svg) {
    flex-shrink: 0;
  }
  .preview-advanced {
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-1);
  }
  .advanced-fields {
    padding: 4px 20px 20px;
  }
  .custom-toggle {
    flex-direction: row;
    align-items: center;
    gap: 9px;
    margin: 8px 0 22px;
  }
  .custom-toggle input {
    width: 14px;
    height: 14px;
    accent-color: var(--accent);
  }
  .setup-progress {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 18px;
    margin-top: 30px;
    font-size: 11px;
  }
  .setup-progress > span {
    display: flex;
    align-items: center;
    gap: 7px;
  }
  .setup-progress > span > span {
    border: 1px solid var(--border-strong);
    border-radius: 50%;
    width: 21px;
    height: 21px;
    display: grid;
    place-items: center;
    font-size: 10px;
  }
  .setup-progress .complete {
    color: var(--success);
  }
  .starting-icon {
    display: inline-flex;
    padding: 16px;
    color: var(--accent);
    background: var(--accent-bg);
    border-radius: 12px;
  }
  .preview-logs {
    border-top: 1px solid var(--border);
  }
  summary {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    color: var(--text-2);
    font-size: 12px;
    padding: 14px 20px;
  }
  summary::after {
    content: "+";
    margin-left: auto;
    color: var(--text-3);
  }
  details[open] summary::after {
    content: "−";
  }
  summary span {
    color: var(--text-3);
    font: 11px var(--font-mono);
  }
  pre {
    margin: 0;
    padding: 0 20px 20px;
    max-height: 260px;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font: 11px/1.7 var(--font-mono);
    color: var(--text-2);
  }
  footer {
    padding: 12px 20px;
    border-top: 1px solid var(--border);
    color: var(--text-3);
    font-size: 11px;
    line-height: 1.6;
  }
  @media (max-width: 620px) {
    .preview-toolbar {
      padding: 14px;
    }
    .preview-setup {
      padding: 20px 16px;
    }
    .setup-fields {
      grid-template-columns: 1fr 100px;
      gap: 12px;
    }
    .preview-actions {
      gap: 7px;
    }
    .preview-error {
      align-items: start;
      flex-direction: column;
    }
    .public-label {
      display: none;
    }
  }
</style>
