<script lang="ts">
  import { Check, Copy, Link, Trash2, Users, X } from "@lucide/svelte";
  import { api, type Project } from "./types";
  type Member = { id: string; name: string; email: string; role: string };
  type Invitation = {
    id: string;
    email: string;
    role: string;
    expires: number;
  };
  let dialog = $state<HTMLDialogElement>();
  let project = $state<Project>();
  let members = $state<Member[]>([]);
  let invitations = $state<Invitation[]>([]);
  let role = $state("viewer");
  let email = $state("");
  let inviteRole = $state("developer");
  let link = $state("");
  let copied = $state(false);
  let busy = $state(false);
  let error = $state("");
  export async function show(value: Project) {
    project = value;
    email = "";
    link = "";
    error = "";
    members = [];
    invitations = [];
    dialog?.showModal();
    await act(load);
  }
  async function load() {
    if (!project) return;
    const data = await api<{ members: Member[]; role: string }>(
      `/projects/${project.id}/members`,
    );
    members = data.members;
    role = data.role;
    invitations =
      role === "owner"
        ? await api<Invitation[]>(`/projects/${project.id}/invitations`)
        : [];
  }
  async function act(action: () => Promise<void>) {
    if (busy) return;
    busy = true;
    error = "";
    try {
      await action();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }
  async function invite() {
    await act(async () => {
      const value = await api<{ url: string }>(
        `/projects/${project!.id}/invitations`,
        "POST",
        { email, role: inviteRole },
      );
      link = value.url;
      copied = false;
      email = "";
      await load();
    });
  }
  async function change(member: Member, next: string | null) {
    await act(async () => {
      try {
        await api(
          `/projects/${project!.id}/members/${member.id}`,
          next ? "PUT" : "DELETE",
          next ? { role: next } : undefined,
        );
      } finally {
        await load();
      }
    });
  }
</script>

<dialog
  bind:this={dialog}
  class="repository-dialog team-dialog"
  aria-labelledby="members-title"
  oncancel={(event) => {
    if (busy) event.preventDefault();
  }}
>
  <header class="repository-dialog-header">
    <div>
      <h2 id="members-title">Project members</h2>
      <p>{project?.name}</p>
    </div>
    <button
      class="icon-button"
      aria-label="Close project members"
      disabled={busy}
      onclick={() => dialog?.close()}><X size={18} /></button
    >
  </header>
  <div class="team-body">
    <p class="team-help">
      Owners manage access. Maintainers can accept and merge changes. Developers
      can run tasks and leave reviews. Viewers can inspect work.
    </p>
    {#each members as member (member.id)}
      <div class="member-row">
        <span class="member-avatar"><Users size={16} /></span>
        <div><strong>{member.name}</strong><small>{member.email}</small></div>
        {#if role === "owner"}<select
            aria-label={"Role for " + member.name}
            value={member.role}
            disabled={busy}
            onchange={(event) => change(member, event.currentTarget.value)}
            ><option value="owner">Owner</option><option value="maintainer"
              >Maintainer</option
            ><option value="developer">Developer</option><option value="viewer"
              >Viewer</option
            ></select
          ><button
            class="icon-button"
            aria-label={"Remove " + member.name + " from project"}
            disabled={busy}
            onclick={() => change(member, null)}><Trash2 size={15} /></button
          >{:else}<span class="member-role">{member.role}</span>{/if}
      </div>
    {/each}
    {#if role === "owner"}
      <form
        class="invite-form"
        onsubmit={(event) => {
          event.preventDefault();
          void invite();
        }}
      >
        <h3>Invite someone</h3>
        <label
          >Email address<input
            type="email"
            bind:value={email}
            required
            maxlength="254"
            placeholder="teammate@example.com"
            disabled={busy}
          /></label
        >
        <label
          >Project role<select bind:value={inviteRole} disabled={busy}
            ><option value="developer">Developer</option><option
              value="maintainer">Maintainer</option
            ><option value="viewer">Viewer</option></select
          ></label
        >
        <button class="primary" disabled={busy || !email.trim()}
          ><Link size={15} />Create invite link</button
        >
      </form>
      {#if link}<div class="invite-link" role="status">
          <p>Share this link with your teammate. It expires in 48 hours.</p>
          <input readonly value={link} aria-label="Invitation link" /><button
            onclick={() =>
              act(async () => {
                await navigator.clipboard.writeText(link);
                copied = true;
              })}
            >{#if copied}<Check size={14} />Copied{:else}<Copy size={14} />Copy
              link{/if}</button
          >
        </div>{/if}
      {#if invitations.length}<h3>Pending invitations</h3>
        {#each invitations as invitation}<div class="member-row">
            <div>
              <strong>{invitation.email}</strong><small
                >{invitation.role} · {invitation.expires * 1000 < Date.now()
                  ? "Expired"
                  : "Expires " +
                    new Date(
                      invitation.expires * 1000,
                    ).toLocaleDateString()}</small
              >
            </div>
            <button
              disabled={busy}
              onclick={() =>
                act(async () => {
                  await api(
                    `/projects/${project!.id}/invitations/${invitation.id}`,
                    "DELETE",
                  );
                  await load();
                })}>Revoke</button
            >
          </div>{/each}{/if}
    {/if}
  </div>
  <footer class="team-footer">
    {#if error}<p class="error" role="alert">{error}</p>{/if}<button
      disabled={busy}
      onclick={() => dialog?.close()}>Done</button
    >
  </footer>
</dialog>

<style>
  .team-dialog[open] {
    display: flex;
    flex-direction: column;
  }
  .repository-dialog-header > div {
    flex: 1;
  }
  .team-body {
    padding: 0 24px 24px;
    overflow-y: auto;
    min-height: 0;
  }
  .team-help {
    font-size: 12px;
    line-height: 1.7;
    color: var(--text-3);
    margin: 0 0 18px;
  }
  .member-row {
    display: flex;
    gap: 12px;
    align-items: center;
    padding: 14px 0;
    border-bottom: 1px solid var(--border);
  }
  .member-row > div {
    flex: 1;
    min-width: 0;
  }
  strong,
  small {
    display: block;
    overflow-wrap: anywhere;
  }
  strong {
    font-size: 12px;
  }
  small {
    font-size: 11px;
    margin-top: 5px;
    color: var(--text-3);
  }
  select {
    font-size: 12px;
    padding: 8px;
    max-width: 135px;
  }
  .member-avatar {
    color: var(--text-3);
  }
  .member-role {
    text-transform: capitalize;
    font-size: 12px;
    color: var(--text-3);
  }
  .invite-form {
    display: flex;
    flex-wrap: wrap;
    align-items: end;
    gap: 12px;
    margin: 24px 0;
  }
  h3 {
    width: 100%;
    font-size: 13px;
    margin: 0 0 5px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 8px;
    font-size: 12px;
  }
  label:first-of-type {
    flex: 1;
    min-width: 160px;
  }
  input {
    min-width: 0;
    width: 100%;
  }
  .invite-link {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    padding: 14px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin: 18px 0 24px;
  }
  .invite-link p {
    width: 100%;
    margin: 0;
    font-size: 12px;
    color: var(--text-2);
  }
  .invite-link input {
    flex: 1;
    min-width: 150px;
    font: 11px var(--font-mono);
  }
  .team-footer {
    padding: 16px 24px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    align-items: end;
    gap: 12px;
  }
  .error {
    align-self: stretch;
    font-size: 12px;
  }
  @media (max-width: 600px) {
    .team-body {
      padding: 0 16px 16px;
    }
    .member-row {
      flex-wrap: wrap;
    }
    .member-row > div {
      flex-basis: 70%;
    }
  }
</style>
