import { useEffect, useState, useSyncExternalStore } from "react";
import { runApi, type TaskRun, type RunUsage, type QueuedTask } from "@/lib/api";
import { useAuth } from "@/lib/auth-context-store";
import { sessionStore } from "@/lib/session-store";
import { chatStore } from "@/lib/chat-store";

const terminalStatuses = new Set(["completed", "cancelled", "failed_terminal", "blocked_corrupt", "blocked_incompatible"]);
const labels: Record<string, string> = {
  running: "Running", pending: "Pending", awaiting_user: "Waiting for approval",
  completed: "Completed", cancelled: "Cancelled", failed_terminal: "Failed",
  blocked_corrupt: "Recovery blocked", blocked_incompatible: "Recovery blocked",
};

function RunProgress({ run, sessionId }: { run: TaskRun; sessionId: string }) {
  const [progress, setProgress] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [usage, setUsage] = useState<RunUsage | null>(null);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function refreshUsage() {
      try {
        const next = await runApi.usage(sessionId, run.run_id);
        if (!disposed) setUsage(next);
      } catch {
        // Progress and action errors are shown separately; a later poll retries usage.
      } finally {
        if (!disposed && !terminalStatuses.has(run.status)) timer = setTimeout(refreshUsage, 2000);
      }
    }
    void refreshUsage();
    return () => { disposed = true; clearTimeout(timer); };
  }, [run.run_id, run.status, sessionId]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let text = "";
    let cursor = "";
    let finished = false;
    async function observe() {
      try {
        const response = await runApi.events(sessionId, run.run_id, controller.signal, cursor || "0");
        if (controller.signal.aborted) return;
        setError(null);
        const reader = response.body?.getReader();
        if (!reader) throw new Error("Task progress is unavailable");
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          buffer += decoder.decode(value, { stream: !done });
          const frames = buffer.split(/\r?\n\r?\n/);
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            const data = frame.split(/\r?\n/).find((line) => line.startsWith("data: "));
            const eventId = frame.split(/\r?\n/).find((line) => line.startsWith("id: "))?.slice(4);
            if (!data || data.slice(6) === "[DONE]") continue;
            if (eventId && Number(eventId) <= Number(cursor)) continue;
            if (eventId) cursor = eventId;
            const part = JSON.parse(data.slice(6)) as { type?: string; delta?: string; toolName?: string; errorText?: string };
            if (part.type === "text-delta") text += part.delta ?? "";
            if (part.type === "tool-input-available") text += `\nUsing ${part.toolName ?? "tool"}…\n`;
            if (part.type === "error") setError(part.errorText ?? "Task failed");
            if (part.type === "finish") finished = true;
            if (text.length > 24000) text = text.slice(-24000);
            setProgress(text);
          }
          if (done) break;
        }
        if (!controller.signal.aborted) {
          setUsage(await runApi.usage(sessionId, run.run_id));
          if (finished && chatStore.getActiveRun()?.runId !== run.run_id && sessionStore.getSnapshot().currentId === sessionId) {
            sessionStore.requestSessionHydration(sessionId);
          }
          if (!finished && !terminalStatuses.has(run.status)) timer = setTimeout(observe, 2000);
        }
      } catch (failure) {
        if (!controller.signal.aborted) {
          setError(failure instanceof Error ? failure.message : "Could not reconnect to task");
          if (!terminalStatuses.has(run.status)) timer = setTimeout(observe, 2000);
        }
      }
    }
    void observe();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [run.run_id, run.status, sessionId]);

  return (
    <div className="mt-2 space-y-2">
      {progress && <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-background p-3 text-xs text-foreground" aria-label="Task progress">{progress}</pre>}
      {usage && <p className="text-xs text-muted-foreground">
        {usage.estimated ? "Estimated " : ""}{(usage.total_tokens ?? 0).toLocaleString()} tokens
        {usage.limits && ` / ${usage.limits.max_run_tokens.toLocaleString()} limit · ${usage.limits.max_run_seconds}s time limit`}
        {usage.estimated_cost != null && ` · Estimated cost $${usage.estimated_cost.toFixed(4)}${usage.cost_complete ? "" : " (partially priced)"}`}
      </p>}
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
    </div>
  );
}

export function TaskPanel() {
  const { userId, accountStatus } = useAuth();
  const { currentId } = useSyncExternalStore(sessionStore.subscribe, sessionStore.getSnapshot);
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [queued, setQueued] = useState<QueuedTask[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      if (!currentId || !userId || accountStatus !== "active") return;
      try {
        const [next, pending] = await Promise.all([runApi.list(currentId), runApi.queued(currentId)]);
        if (!disposed) {
          setRuns(next); setQueued(pending); setError(null);
          setSelected((previous) => previous ?? next.find((run) => !terminalStatuses.has(run.status))?.run_id ?? null);
        }
      } catch (failure) {
        if (!disposed) setError(failure instanceof Error ? failure.message : "Could not load tasks");
      } finally {
        if (!disposed) timer = setTimeout(refresh, 2000);
      }
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timer); };
  }, [currentId, userId, accountStatus]);

  if (!currentId || accountStatus !== "active") return null;
  const active = runs.find((run) => !terminalStatuses.has(run.status));
  const expanded = runs.find((run) => run.run_id === selected) ?? active;

  async function control(action: "steer" | "queue" | "cancel") {
    if (!currentId || busy) return;
    const scope = currentId;
    setBusy(true); setError(null); setNotice(null);
    try {
      if (action === "cancel" && active) {
        const response = await runApi.cancel(active.run_id, scope);
        await response.text();
      } else if (action === "steer" && active) {
        await runApi.steer(scope, active.run_id, message.trim());
      } else if (action === "queue") {
        await runApi.queue(scope, message.trim());
      }
      if (sessionStore.getSnapshot().currentId !== scope) return;
      setMessage("");
      setNotice(action === "steer" ? "Instruction added for the next model call." : action === "queue" ? "Follow-up queued." : "Cancellation requested.");
      setRuns(await runApi.list(scope));
    } catch (failure) {
      if (sessionStore.getSnapshot().currentId === scope) setError(failure instanceof Error ? failure.message : "Task action failed");
    } finally { setBusy(false); }
  }

  return (
    <details className="border-b border-border bg-surface px-4 py-2" open={Boolean(active || expanded || notice || error)}>
      <summary className="cursor-pointer text-sm text-muted-foreground">Tasks{active ? ` · ${labels[active.status] ?? active.status}` : ` · ${runs.length}`}</summary>
      <div className="mt-2 max-h-80 overflow-auto">
        <p className="mb-2 text-xs text-muted-foreground">Tasks continue when you close this page. Reopen this conversation to view progress.</p>
        <div className="flex flex-wrap gap-2">
          {runs.slice(0, 10).map((run) => <button key={run.run_id} className="rounded border border-border px-2 py-1 text-xs hover:border-accent" onClick={() => setSelected(run.run_id)}>{labels[run.status] ?? run.status} · {new Date(run.created_at).toLocaleTimeString()}</button>)}
        </div>
        {queued.filter((task) => task.status !== "completed").slice(0, 10).map((task) => <p key={task.queue_id} className="mt-2 text-xs text-muted-foreground">{task.status}: {task.message.slice(0, 160)}{task.error ? ` · ${task.error}` : ""}</p>)}
        {expanded && <RunProgress key={`${userId}:${currentId}:${expanded.run_id}`} run={expanded} sessionId={currentId} />}
        {active && <div className="mt-3 space-y-2">
          <textarea aria-label="Task instruction or follow-up" placeholder="Add an instruction or queue the next task…" value={message} onChange={(event) => setMessage(event.target.value)} maxLength={10000} rows={2} className="w-full rounded-lg border border-border bg-input p-2 text-sm" />
          <div className="flex flex-wrap gap-2">
            <button disabled={busy || !message.trim() || active.status === "awaiting_user"} onClick={() => void control("steer")} className="rounded border border-border px-3 py-1 text-xs disabled:opacity-40">Add instruction</button>
            <button disabled={busy || !message.trim()} onClick={() => void control("queue")} className="rounded border border-border px-3 py-1 text-xs disabled:opacity-40">Queue follow-up</button>
            <button disabled={busy} onClick={() => void control("cancel")} className="rounded border border-danger/30 px-3 py-1 text-xs text-danger disabled:opacity-40">Cancel task</button>
          </div>
        </div>}
        {notice && <p role="status" className="mt-2 text-xs text-muted-foreground">{notice}</p>}
        {error && <p role="alert" className="mt-2 text-xs text-danger">{error}</p>}
      </div>
    </details>
  );
}
