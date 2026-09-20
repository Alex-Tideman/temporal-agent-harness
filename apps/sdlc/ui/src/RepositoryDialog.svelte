<script lang="ts">
  import { tick } from "svelte";
  import {
    FolderOpen,
    FolderPlus,
    Search,
    X,
    ArrowRight,
    ArrowLeft,
    ChevronRight,
    Check,
    RefreshCw,
    LoaderCircle,
  } from "@lucide/svelte";
  import {
    api,
    type Project,
    type RepositoryDiscovery,
    type FolderListing,
  } from "./types";
  import { matchesRepository, repositoryLocation } from "./repositoryLabels";

  let {
    projects,
    onconnect,
  }: {
    projects: Project[];
    onconnect: (project: Project) => Promise<boolean>;
  } = $props();
  let dialog = $state<HTMLDialogElement>();
  let manual = $state<HTMLDetailsElement>();
  let search = $state<HTMLInputElement>();
  let query = $state("");
  let path = $state("");
  let error = $state("");
  let busy = $state("");
  let loading = $state(false);
  let discovery = $state<RepositoryDiscovery>();
  let listing = $state<FolderListing>();
  let folderPurpose = $state<"repository" | "projects">("repository");
  let browseQuery = $state("");
  let generation = 0;
  let connected = $derived(
    projects.filter((item) => matchesRepository(item, query)),
  );
  let suggestions = $derived(
    discovery?.suggestions.filter(
      (item) =>
        !projects.some((project) => project.path === item.path) &&
        `${item.name} ${item.path}`
          .toLowerCase()
          .includes(query.trim().toLowerCase()),
    ) ?? [],
  );
  let folders = $derived(
    listing?.folders.filter((item) =>
      item.name.toLowerCase().includes(browseQuery.toLowerCase()),
    ) ?? [],
  );

  export async function show() {
    if (manual) manual.open = false;
    query = "";
    path = "";
    error = "";
    listing = undefined;
    dialog?.showModal();
    await tick();
    search?.focus();
    await loadSuggestions();
  }
  async function loadSuggestions() {
    const request = ++generation;
    loading = true;
    try {
      const value = await api<RepositoryDiscovery>("/projects/discovery");
      if (request === generation) discovery = value;
    } catch (e) {
      if (request === generation) error = (e as Error).message;
    } finally {
      if (request === generation) loading = false;
    }
  }
  async function connect(value: string, existing?: Project) {
    if (busy) return;
    busy = "Opening repository…";
    error = "";
    try {
      const project =
        existing ?? (await api<Project>("/projects", "POST", { path: value }));
      if (await onconnect(project)) dialog?.close();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = "";
    }
  }
  async function browse(value = "") {
    busy = "Opening folder…";
    error = "";
    try {
      listing = await api<FolderListing>(
        "/projects/folders?path=" + encodeURIComponent(value),
      );
      browseQuery = "";
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = "";
    }
  }
  async function useFolder(value: string) {
    if (folderPurpose === "repository") {
      await connect(value);
      return;
    }
    busy = "Finding repositories…";
    error = "";
    ++generation;
    try {
      discovery = await api<RepositoryDiscovery>(
        "/projects/discovery",
        "POST",
        { path: value },
      );
      listing = undefined;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = "";
      loading = false;
    }
  }
  async function pick(purpose: "repository" | "projects") {
    if (busy) return;
    folderPurpose = purpose;
    busy = "Choose a folder in the window that opened…";
    error = "";
    try {
      const result = await api<{ native: boolean; path: string | null }>(
        "/projects/pick-folder",
        "POST",
        {},
      );
      busy = "";
      if (result.path) await useFolder(result.path);
      else if (!result.native) await browse(discovery?.folder || "");
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = "";
    }
  }
  async function forgetFolder() {
    busy = "Removing projects folder…";
    error = "";
    ++generation;
    try {
      discovery = await api<RepositoryDiscovery>(
        "/projects/discovery",
        "DELETE",
      );
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = "";
      loading = false;
    }
  }
</script>

<dialog
  bind:this={dialog}
  class="repository-dialog"
  aria-labelledby="repository-dialog-title"
  aria-describedby="repository-dialog-description"
  oncancel={(event) => {
    if (busy) event.preventDefault();
  }}
  onclose={() => {
    ++generation;
  }}
>
  <header class="repository-dialog-header">
    <div class="repository-dialog-icon"><FolderPlus size={20} /></div>
    <div>
      <h2 id="repository-dialog-title">
        {listing ? "Choose a folder" : "Add repository"}
      </h2>
      <p id="repository-dialog-description">
        {listing
          ? folderPurpose === "projects"
            ? "Choose where you keep your projects."
            : "Choose a Git repository or one of its subfolders."
          : "Choose a project. Get straight to work."}
      </p>
    </div>
    <button
      type="button"
      class="icon-button"
      aria-label="Close repository dialog"
      disabled={!!busy}
      onclick={() => dialog?.close()}><X size={18} /></button
    >
  </header>
  {#if error}<div class="repository-error" role="alert">{error}</div>{/if}
  {#if busy}<div class="repository-progress" role="status">
      <LoaderCircle size={15} class="repository-spinner" />{busy}
    </div>{/if}
  {#if listing}
    <div class="repository-browser">
      <button
        type="button"
        class="text-button"
        disabled={!!busy}
        onclick={() => {
          listing = undefined;
          error = "";
        }}><ArrowLeft size={14} />Back to repositories</button
      >
      <nav class="folder-breadcrumbs" aria-label="Folder location">
        {#each listing.parents as parent}<button
            type="button"
            title={parent.path}
            disabled={!!busy || parent.path === listing.path}
            onclick={() => browse(parent.path)}>{parent.name}</button
          ><ChevronRight size={12} />{/each}
      </nav>
      <div class="repository-search">
        <Search size={16} /><input
          bind:value={browseQuery}
          aria-label="Filter folders"
          placeholder="Filter folders…"
        />
      </div>
      <div class="folder-list">
        {#each folders as folder}<button
            type="button"
            class="repository-option"
            disabled={!!busy}
            onclick={() => browse(folder.path)}
            aria-label={`Open folder ${folder.name}`}
            ><FolderOpen size={17} /><span
              ><strong>{folder.name}</strong>{#if folder.repository}<small
                  >Git repository</small
                >{/if}</span
            ><ChevronRight size={15} /></button
          >{:else}<p class="repository-empty">
            {browseQuery ? "No matching folders." : "No visible subfolders."}
          </p>{/each}
      </div>
      {#if listing.truncated}<p class="repository-empty">
          Showing the first folders. Use Enter path to open another location.
        </p>{/if}
      <footer class="folder-actions">
        <span title={listing.path}>{listing.display_path}</span><button
          type="button"
          class="primary"
          disabled={!!busy}
          onclick={() => listing && useFolder(listing.path)}
          >Use this folder<ArrowRight size={15} /></button
        >
      </footer>
    </div>
  {:else}
    <div class="repository-dialog-actions">
      <button
        type="button"
        class="primary"
        disabled={!!busy}
        onclick={() => pick("repository")}
        ><FolderOpen size={16} />Choose folder<ArrowRight size={15} /></button
      ><span>From this computer</span>
    </div>
    <div class="repository-search dialog-search">
      <Search size={16} /><input
        bind:this={search}
        bind:value={query}
        aria-label="Search local repositories"
        placeholder="Find a repository…"
        autocomplete="off"
      />
    </div>
    <div class="repository-dialog-results" aria-busy={loading}>
      {#if connected.length}<section aria-label="Connected repositories">
          <div class="repository-section-heading">
            <span>Connected repositories</span>
          </div>
          {#each connected as project}<button
              type="button"
              class="repository-option"
              disabled={!!busy}
              onclick={() => connect(project.path, project)}
              aria-label={`Open ${project.name} — ${repositoryLocation(project)}`}
              ><FolderOpen size={17} /><span
                ><strong>{project.name}</strong><small title={project.path}
                  >{repositoryLocation(project)}</small
                ></span
              ><Check size={14} /><ArrowRight size={15} /></button
            >{/each}
        </section>{/if}
      <section aria-label="Suggested repositories">
        <div class="repository-section-heading">
          <span>Suggested repositories</span>{#if discovery?.folder}<button
              type="button"
              class="icon-button"
              aria-label="Refresh suggested repositories"
              disabled={!!busy || loading}
              onclick={loadSuggestions}><RefreshCw size={13} /></button
            >{/if}
        </div>
        {#if discovery?.folder}<div class="projects-folder">
            <span title={discovery.folder}>{discovery.display_path}</span
            ><button
              type="button"
              disabled={!!busy}
              onclick={() => pick("projects")}>Change</button
            ><button
              type="button"
              class="icon-button"
              aria-label="Forget projects folder"
              disabled={!!busy}
              onclick={forgetFolder}><X size={13} /></button
            >
          </div>{/if}
        {#if loading}<p class="repository-empty">Finding repositories…</p>
        {:else if discovery?.folder}
          {#each suggestions as item}<button
              type="button"
              class="repository-option"
              disabled={!!busy}
              onclick={() => connect(item.path)}
              aria-label={`Add ${item.name} — ${item.display_path}`}
              ><FolderOpen size={17} /><span
                ><strong>{item.name}</strong><small title={item.path}
                  >{item.display_path}</small
                ></span
              ><ArrowRight size={15} /></button
            >{:else}<p class="repository-empty">
              {discovery.warning ||
                (query
                  ? "No matching repositories in this folder."
                  : "No new repositories found. Choose a repository folder directly, or change your projects folder.")}
            </p>{/each}
          {#if discovery.truncated}<p class="repository-empty">
              Showing a limited scan. Choose a more specific projects folder to
              find more.
            </p>{/if}
        {:else}<div class="repository-discovery-empty">
            <FolderPlus size={20} />
            <p>
              Keep your projects together?<span
                >Choose their parent folder once to see suggestions here.</span
              >
            </p>
            <button
              type="button"
              disabled={!!busy}
              onclick={() => pick("projects")}>Choose projects folder</button
            >
          </div>{/if}
      </section>
      {#if query && !connected.length && !suggestions.length && !loading}<p
          class="repository-empty"
        >
          Can't find it? Choose its folder above.
        </p>{/if}
    </div>
  {/if}
  <details class="repository-manual" bind:this={manual}>
    <summary>Enter path</summary>
    <form
      onsubmit={(event) => {
        event.preventDefault();
        if (listing) useFolder(path);
        else connect(path);
      }}
    >
      <label for="repository-path"
        >{listing && folderPurpose === "projects"
          ? "Projects folder"
          : "Repository folder"}</label
      >
      <div>
        <input
          id="repository-path"
          bind:value={path}
          placeholder="~/Projects/my-project"
          required
          disabled={!!busy}
        /><button type="submit" disabled={!!busy || !path.trim()}
          >Open<ArrowRight size={14} /></button
        >
      </div>
    </form>
  </details>
</dialog>
