<script lang="ts">
  import { tick } from "svelte";
  import { FolderOpen, Search, Check, Plus, ChevronDown } from "@lucide/svelte";
  import type { Project } from "./types";
  import { matchesRepository, repositoryLocation } from "./repositoryLabels";

  let {
    projects,
    value = $bindable(""),
    onadd,
    disabled = false,
  }: {
    projects: Project[];
    value?: string;
    onadd: () => void;
    disabled?: boolean;
  } = $props();
  let trigger = $state<HTMLButtonElement>();
  let popup = $state<HTMLDivElement>();
  let input = $state<HTMLInputElement>();
  let open = $state(false);
  let query = $state("");
  const id = "task-repository-picker";
  let selected = $derived(projects.find((project) => project.id === value));
  let results = $derived(
    projects.filter((project) => matchesRepository(project, query)),
  );

  function position() {
    if (!open || !trigger || !popup) return;
    const anchor = trigger.getBoundingClientRect();
    const width = Math.min(360, window.innerWidth - 24);
    popup.style.width = width + "px";
    popup.style.left =
      Math.max(12, Math.min(anchor.left, window.innerWidth - width - 12)) +
      "px";
    const below = window.innerHeight - anchor.bottom - 20;
    const above = anchor.top - 20;
    const useAbove = below < 300 && above > below;
    popup.style.maxHeight =
      Math.max(140, Math.min(440, useAbove ? above : below)) + "px";
    popup.style.top = useAbove ? "auto" : anchor.bottom + 8 + "px";
    popup.style.bottom = useAbove
      ? window.innerHeight - anchor.top + 8 + "px"
      : "auto";
  }
  async function toggle() {
    if (open) {
      popup?.hidePopover();
      return;
    }
    query = "";
    popup?.showPopover();
    open = true;
    await tick();
    position();
    input?.focus();
  }
  function choose(project: Project) {
    value = project.id;
    popup?.hidePopover();
    trigger?.focus();
  }
  function keys(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      popup?.hidePopover();
      trigger?.focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const buttons = Array.from(
        popup?.querySelectorAll<HTMLButtonElement>("button[data-result]") ?? [],
      );
      const index = buttons.indexOf(
        document.activeElement as HTMLButtonElement,
      );
      const next =
        event.key === "ArrowDown"
          ? index + 1
          : index < 0
            ? buttons.length - 1
            : index - 1;
      if (next < 0) input?.focus();
      else buttons[Math.min(next, buttons.length - 1)]?.focus();
    } else if (event.key === "Enter" && event.target === input) {
      event.preventDefault();
      event.stopPropagation();
      if (results[0]) choose(results[0]);
    }
  }
</script>

<svelte:window onresize={position} onscroll={position} />
<button
  type="button"
  class="repository-trigger"
  bind:this={trigger}
  onclick={toggle}
  {disabled}
  aria-label="Task repository"
  aria-haspopup="dialog"
  aria-expanded={open}
  aria-controls={id}
>
  <FolderOpen size={14} /><span>{selected?.name ?? "Select repository"}</span
  ><ChevronDown size={14} />
</button>
<div
  {id}
  bind:this={popup}
  popover="auto"
  class="repository-popover"
  role="dialog"
  aria-label="Select repository"
  tabindex="-1"
  ontoggle={(event) => {
    open = (event as ToggleEvent).newState === "open";
  }}
  onkeydown={keys}
>
  <div class="repository-search">
    <Search size={16} /><input
      bind:this={input}
      bind:value={query}
      aria-label="Search repositories"
      placeholder="Find a repository…"
      autocomplete="off"
    />
  </div>
  <div class="repository-popover-list">
    <span class="repository-list-label"
      >{query
        ? `${results.length} ${results.length === 1 ? "repository" : "repositories"}`
        : "Your repositories"}</span
    >
    {#each results as project}
      <button
        type="button"
        class="repository-option"
        data-result
        onclick={() => choose(project)}
        aria-label={`Select ${project.name} — ${repositoryLocation(project)}`}
      >
        <FolderOpen size={16} /><span
          ><strong>{project.name}</strong><small title={project.path}
            >{repositoryLocation(project)}</small
          ></span
        >{#if project.id === value}<Check size={15} />{/if}
      </button>
    {:else}<p class="repository-empty">
        {query
          ? "No matching repositories."
          : "Connect a repository to get started."}
      </p>{/each}
  </div>
  <button
    type="button"
    data-result
    class="repository-add-action"
    onclick={() => {
      popup?.hidePopover();
      onadd();
    }}><Plus size={15} />Add repository…</button
  >
</div>
