export interface Person {
  id: string;
  name: string;
}
export interface Account extends Person {
  email: string | null;
  admin: number | boolean;
}
export interface Profile {
  available?: boolean;
  id: string;
  label: string;
  provider: string;
  model: string;
  credential: string;
  max_steps: number;
}
export interface ProviderCheck {
  ok: boolean;
  stage: "credential" | "request" | "response";
  title: string;
  explanation: string;
  actions: string[];
  endpoint: string;
  credential_source: string;
  elapsed_ms: number;
  http_status: number | null;
  provider_code: string;
  provider_message: string;
  parameter: string;
}
export interface Project {
  role?: "owner" | "maintainer" | "developer" | "viewer";
  remote_url?: string;
  default_branch?: string;
  id: string;
  name: string;
  path: string;
  github_url: string;
  last_opened?: number;
  demo?: boolean;
}
export interface RepositoryDiscovery {
  folder: string;
  display_path: string;
  suggestions: {
    name: string;
    path: string;
    display_path: string;
    connected_id: string;
  }[];
  truncated: boolean;
  warning: string;
}
export interface FolderListing {
  path: string;
  display_path: string;
  parents: { name: string; path: string }[];
  folders: { name: string; path: string; repository: boolean }[];
  truncated: boolean;
}
export interface State {
  engine?: string;
  verification?: string;
  revision?: string;
  review_revision?: string;
  repair_attempt?: number;
  host_calls?: number;
  acceptance_note?: string;
  project_merge?: ProjectMerge | null;
  actors?: {
    id: string;
    role: string;
    status: string;
    operation: string;
    model_calls: number;
    input_tokens: number;
    output_tokens: number;
    host_calls: number;
  }[];
  blueprints?: {
    revision: number;
    approved: boolean;
    summary: string;
    requirements: string[];
    assumptions: string[];
    scope: string[];
  }[];
  findings?: {
    severity: string;
    path: string;
    line: number;
    explanation: string;
  }[];
  missing_evidence?: string[];
  mode: string;
  phase: string;
  status: string;
  focus: string;
  next_action: string;
  model: string;
  model_calls: number;
  max_steps: number;
  input_tokens: number;
  output_tokens: number;
  steps: { id: string; title: string; status: string }[];
  risks: string[];
  gate: { id: string; kind: string; title: string; detail: string } | null;
  entries: { id: string; role: string; text: string }[];
  checks: {
    id: string;
    command: string;
    exit_code: number;
    output: string;
    stale: boolean;
    revision?: string;
    after_revision?: string;
    duration_ms?: number;
  }[];
  files_changed: string[];
  outcome: string;
  failure?: {
    category: string;
    title: string;
    explanation: string;
    actions: string[];
    http_status?: number | null;
    provider_code: string;
  } | null;
}
export interface Task {
  workspace_layout?: "project-worktree";
  owner?: Person;
  ownership_version?: number;
  can_handoff?: boolean;
  worktree_branch?: string;
  preview_port?: number;
  sandbox_status?: string;
  checkpoint?: { saved_at: number; revision: string };
  created_by?: Person;
  can_control?: boolean;
  can_review?: boolean;
  can_accept?: boolean;
  can_merge?: boolean;
  can_manage?: boolean;
  engine?: string;
  id: string;
  workflow_id: string;
  project_id: string;
  project_name: string;
  title: string;
  mode: string;
  profile_id: string;
  input?: { prompt: string; profile: Profile };
  preparation_error?: string;
  preparation_running?: boolean;
  dispatch_error?: string;
  workspace?: string;
  workspace_backend?: "e2b" | "local";
  sandbox_id?: string;
  sandbox_template?: string;
  sandbox_environment?: {
    template: string;
    label: string;
    source: "automatic" | "override" | "fallback";
    reason: string;
  };
  preparation_ms?: number;
  base_commit?: string;
  source_dirty?: boolean;
  dispatch: string;
  created_at: number;
  version: number;
  updated: number;
  live?: boolean;
  can_message?: boolean;
  next_turn?: number;
  pending_message?: {
    id: string;
    payload: { prompt: string };
    error?: string;
  };
  message_error?: MessageReceipt & { prompt: string };
  snapshot: {
    version: number;
    value: State;
    run_id: string;
    workflow_id: string;
    conversation?: {
      version: number;
      value: { turns: ConversationTurn[]; context_shortened: boolean };
    };
  } | null;
}
export interface ProjectMerge {
  ok: boolean;
  operation_id: string;
  base_revision: string;
  revision: string;
  files: string[];
  applied_files: string[];
  already_present_files: string[];
  already_applied: boolean;
  project_path: string;
  branch: string;
  merged_at: number;
}
export interface ConversationTurn {
  number: number;
  message_id: string;
  prompt: string;
  mode: string;
  profile_id: string;
  profile_label: string;
  provider: string;
  model: string;
  max_steps: number;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  status: string;
  outcome: string;
}
export interface MessageDraft {
  prompt: string;
  mode: string;
  profile_id: string;
  max_steps: number;
  attempt?: { id: string; expected_turn: number };
}
export interface MessageReceipt {
  id: string;
  status: "accepted" | "pending" | "rejected";
  turn_number?: number;
  error?: string;
}
export interface Home {
  shared?: boolean;
  user?: Account;
  profiles: Profile[];
  projects: Project[];
  tasks: Task[];
  health: {
    temporal: boolean;
    harness_version: string;
    data_dir: string;
    execution: string;
    sandbox_ready: boolean;
    sandbox_template: string;
    milestone: string;
  };
}
export interface ReviewComment {
  author?: Person;
  resolved_by?: Person;
  id: string;
  path: string;
  side: "file" | "old" | "new";
  line: number;
  text: string;
  excerpt: string;
  source_token: string;
  comparison: "all" | "reviewed";
  created_at: number;
  resolved: boolean;
  outdated: boolean;
}
export interface ReviewSummary {
  version: number;
  base_commit: string;
  omitted: number;
  files: {
    path: string;
    token: string;
    status: string;
    issue: string;
    reviewed: boolean;
    reviewed_by?: Person[];
    has_checkpoint: boolean;
    since_review: boolean;
    additions: number;
    deletions: number;
    review_additions?: number;
    review_deletions?: number;
  }[];
  comments: ReviewComment[];
}
export interface ReviewDetail {
  path: string;
  token: string;
  source_token: string;
  comparison: "all" | "reviewed";
  baseline: string;
  reviewed_at: number | null;
  issue: string;
  truncated: boolean;
  changed: boolean;
  old_mode: string;
  new_mode: string;
  old_exists: boolean;
  new_exists: boolean;
  old_final_newline: boolean;
  new_final_newline: boolean;
  lines: { kind: string; text: string; old: number; new: number }[];
}
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch("/api" + path, {
    method,
    headers: { "Content-Type": "application/json", "X-SDLC": "1" },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    const error = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new ApiError(
      typeof error.detail === "string"
        ? error.detail
        : JSON.stringify(error.detail),
      response.status,
    );
  }
  return response.json();
}
