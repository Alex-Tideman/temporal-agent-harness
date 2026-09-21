<script lang="ts">
  import { ArrowRight, Brain } from "@lucide/svelte";
  import { api } from "./types";
  let {
    setup = false,
    token = "",
    invite = "",
    onauthenticated,
    oncancel,
  }: {
    setup?: boolean;
    token?: string;
    invite?: string;
    onauthenticated: () => Promise<void>;
    oncancel?: () => void;
  } = $props();
  let email = $state("");
  let name = $state("");
  let password = $state("");
  let setupToken = $state("");
  let busy = $state(false);
  let error = $state("");
  let creating = $derived(setup || !!invite);
  async function submit() {
    if (busy) return;
    busy = true;
    error = "";
    try {
      await api(
        "/auth/" + (setup ? "bootstrap" : invite ? "join" : "login"),
        "POST",
        {
          email,
          password,
          ...(creating ? { name, token: invite || token || setupToken } : {}),
        },
      );
      password = "";
      await onauthenticated();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }
</script>

<main class="unlock account-access">
  <div class="brandmark" aria-hidden="true"><Brain size={23} /></div>
  <span class="eyebrow">YOUR SHARED DEVELOPMENT WORKSPACE</span>
  <h1>
    {setup
      ? "Create your workspace account"
      : invite
        ? "Join your project"
        : "Welcome back"}
  </h1>
  <p>
    {setup
      ? "Create the owner account to manage projects and invite your team."
      : invite
        ? "Use the email address on your invitation. Already have an account? Use your existing password."
        : "Sign in to work with your team and review their changes."}
  </p>
  <form
    onsubmit={(event) => {
      event.preventDefault();
      void submit();
    }}
  >
    {#if creating}<label
        >Your name<input
          bind:value={name}
          autocomplete="name"
          required
          maxlength="80"
          disabled={busy}
        /></label
      >{/if}
    <label
      >Email<input
        type="email"
        bind:value={email}
        autocomplete="username"
        required
        maxlength="254"
        disabled={busy}
      /></label
    >
    <label
      >Password<input
        type="password"
        bind:value={password}
        autocomplete={creating ? "new-password" : "current-password"}
        required
        minlength={creating ? 12 : 1}
        maxlength="256"
        disabled={busy}
      /></label
    >
    {#if creating}<p class="account-help">Use at least 12 characters.</p>{/if}
    {#if setup && !token}<label
        >Setup token<input
          type="password"
          bind:value={setupToken}
          required
          autocomplete="off"
          disabled={busy}
        /></label
      >
      <p class="account-help">
        Use the token from the server’s launch URL.
      </p>{/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
    <button class="primary" disabled={busy}
      >{busy
        ? "Signing in…"
        : setup
          ? "Create account"
          : invite
            ? "Join project"
            : "Sign in"}<ArrowRight size={16} /></button
    >
  </form>
  {#if invite}<button class="text-button" disabled={busy} onclick={oncancel}
      >Back to sign in</button
    >{/if}
</main>

<style>
  .account-access {
    max-width: 470px;
  }
  form {
    display: flex;
    flex-direction: column;
    gap: 16px;
    text-align: left;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 7px;
    font-size: 12px;
    color: var(--text-2);
  }
  input {
    width: 100%;
  }
  .account-help {
    margin: -9px 0 0;
    font-size: 11px;
  }
  button {
    min-height: 42px;
  }
</style>
