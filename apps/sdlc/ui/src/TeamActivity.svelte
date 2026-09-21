<script lang="ts">
  import { untrack } from "svelte";
  import { api, type Person } from "./types";
  let { taskId, version }: { taskId: string; version: number } = $props();
  type Event = {
    id: string;
    actor: Person;
    action: string;
    status: string;
    created_at: number;
    detail: { revision?: string; approved?: boolean };
  };
  let events = $state<Event[]>([]);
  let error = $state("");
  let refreshRequest = $state(0);
  const labels: Record<string, string> = {
    "task.created": "started this task",
    "task.handoff": "handed off this task",
    "task.message": "sent a follow-up",
    "task.respond": "responded to an approval",
    "task.accept": "requested acceptance",
    "task.merge": "requested a merge",
    "task.pause": "requested a pause",
    "task.resume": "requested a resume",
    "task.cancel": "requested a stop",
  };
  $effect(() => {
    taskId;
    version;
    refreshRequest;
    let disposed = false;
    untrack(() =>
      api<Event[]>(`/tasks/${taskId}/activity`)
        .then((value) => {
          if (!disposed) {
            events = value;
            error = "";
          }
        })
        .catch((e) => {
          if (!disposed) error = e.message;
        }),
    );
    return () => {
      disposed = true;
    };
  });
</script>

<section class="team-activity" aria-label="Team activity">
  <div class="activity-heading">
    <h2>Team activity</h2>
    <button onclick={() => refreshRequest++}>Refresh</button>
  </div>
  <p>
    Task decisions are recorded with the person who requested them. File review
    progress belongs to each reviewer.
  </p>
  {#if error}<p role="alert">{error}</p>{/if}
  <ol>
    {#each events as event}<li>
        <div>
          <strong>{event.actor.name}</strong>
          {labels[event.action] || event.action}<span class="tiny-pill"
            >{event.status}</span
          >
        </div>
        <small
          >{new Date(
            event.created_at * 1000,
          ).toLocaleString()}{#if event.detail.revision}
            · revision {event.detail.revision.slice(
              0,
              12,
            )}{/if}{#if event.detail.approved !== undefined}
            · {event.detail.approved ? "Approved" : "Declined"}{/if}</small
        >
      </li>{:else}<li>No team decisions recorded yet.</li>{/each}
  </ol>
</section>

<style>
  .activity-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  section {
    padding: 24px;
  }
  h2 {
    font-size: 16px;
  }
  p,
  small {
    color: var(--text-3);
    font-size: 12px;
  }
  ol {
    list-style: none;
    padding: 0;
    margin: 24px 0 0;
  }
  li {
    padding: 16px 0;
    border-top: 1px solid var(--border);
    font-size: 13px;
  }
  li div {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 6px;
  }
  .tiny-pill {
    margin-left: auto;
  }
</style>
