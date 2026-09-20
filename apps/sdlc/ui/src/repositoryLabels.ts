import type { Project } from "./types";

export function repositoryLocation(
  project: Pick<Project, "path" | "github_url">,
) {
  const parts = project.path.split("/").filter(Boolean);
  return parts.length > 3 ? "…/" + parts.slice(-3).join("/") : project.path;
}

export function matchesRepository(
  project: Pick<Project, "name" | "path" | "github_url">,
  query: string,
) {
  return `${project.name} ${project.path} ${project.github_url}`
    .toLowerCase()
    .includes(query.trim().toLowerCase());
}
