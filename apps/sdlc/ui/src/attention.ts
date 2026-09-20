import type { Task } from "./types";

export function taskAttention(task: Task): string {
  const state = task.snapshot?.value;
  if (task.preparation_error) return "Workspace needs attention";
  if (state?.gate) {
    if (state.gate.kind === "command") return "Approve command";
    if (state.gate.kind === "plan")
      return state.engine === "v2" ? "Approve blueprint" : "Approve plan";
    return "Answer question";
  }
  if (task.message_error) return "Retry your message";
  if (state?.status === "failed") return "Resolve task error";
  if (state?.status === "paused") return "Resume task";
  if (state?.status === "review")
    return state.mode === "ask" ? "Review answer" : "Review changes";
  return "";
}
