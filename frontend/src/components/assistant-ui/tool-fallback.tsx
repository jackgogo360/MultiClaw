import { useEffect, useMemo, useRef } from "react";

import { ApprovalToolUI } from "@/components/approval/ApprovalToolUI";

function summarizeArgs(args: Record<string, unknown>): string {
  const raw = JSON.stringify(args);
  if (!raw || raw === "{}") {
    return "No parameters";
  }
  return raw.length > 120 ? `${raw.slice(0, 117)}...` : raw;
}

function summarizeStatus(status: { type?: string; reason?: string } | undefined): string {
  switch (status?.type) {
    case "running":
    case "input-streaming":
    case "input-available":
      return "Running";
    case "complete":
    case "output-available":
      return "Done";
    case "incomplete":
    case "output-error":
    case "output-denied":
      return status.reason ? `Stopped: ${status.reason}` : "Stopped";
    case "requires-action":
    case "approval-requested":
    case "approval-responded":
      return "Needs approval";
    default:
      return "Queued";
  }
}

function summarizePayload(value: unknown): string | null {
  if (value == null) {
    return null;
  }

  if (typeof value === "string") {
    return value;
  }

  if (
    typeof value === "object" &&
    value !== null &&
    "content" in value &&
    typeof (value as { content?: unknown }).content === "string"
  ) {
    return (value as { content: string }).content;
  }

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/**
 * Tool call override component used as `tools.Override` on
 * `MessagePrimitive.Parts`. Renders the approval UI when a tool
 * call has `status.type === "requires-action"`, otherwise passes
 * through for default rendering.
 *
 * Usage:
 *   <MessagePrimitive.Parts
 *     components={{ tools: { Override: ToolFallback } }}
 *   />
 */
export function ToolFallback(props: Record<string, unknown>) {
  const status = props.status as { type?: string; reason?: string } | undefined;
  const state = typeof props.state === "string" ? props.state : undefined;
  const approval = props.approval as
    | { id?: string; approved?: boolean; reason?: string }
    | undefined;
  const args = useMemo(
    () => ((props.args as Record<string, unknown>) ?? {}),
    [props.args]
  );
  const resultSummary = summarizePayload(props.result);
  const isError = props.isError === true;
  const statusLabel = summarizeStatus(status ?? (state ? { type: state } : undefined));
  const logSignature = JSON.stringify({
    toolCallId: props.toolCallId,
    toolName: props.toolName,
    args,
    state,
    status: status?.type,
    resultSummary,
    isError,
  });
  const lastLogSignatureRef = useRef<string>("");

  useEffect(() => {
    if (typeof window === "undefined" || window.location.hostname !== "localhost") {
      return;
    }
    if (lastLogSignatureRef.current === logSignature) {
      return;
    }
    lastLogSignatureRef.current = logSignature;
    console.info("[chat] tool activity", {
      toolCallId: props.toolCallId,
      toolName: props.toolName,
      args,
      state,
      status: status?.type,
      result: resultSummary,
      isError,
    });
  }, [args, isError, logSignature, props.toolCallId, props.toolName, resultSummary, state, status?.type]);

  if (status?.type === "requires-action") {
    return (
      <ApprovalToolUI
        approvalId={approval?.id}
        toolCallId={props.toolCallId as string}
        toolName={props.toolName as string}
        args={(props.args as Record<string, unknown>) ?? {}}
        status={status}
        respondToApproval={
          props.respondToApproval as
            | ((response: { approved: boolean; reason?: string }) => void)
            | undefined
        }
      />
    );
  }

  return (
    <div className="my-1 rounded-lg border border-border/50 bg-elevated/50 px-3 py-2 text-xs">
      <div className="flex items-center gap-3">
        <span className="inline-flex h-2 w-2 shrink-0 rounded-full bg-accent/80" />
        <span className="font-mono text-foreground/90">
          {props.toolName as string}
        </span>
        <span className="truncate text-muted-foreground">
          {summarizeArgs(args)}
        </span>
        <span className="ml-auto shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground/80">
          {statusLabel}
        </span>
      </div>
      {resultSummary ? (
        <pre
          className={`mt-2 whitespace-pre-wrap break-words border-t border-border/40 pt-2 font-mono text-[11px] ${
            isError ? "text-danger" : "text-muted-foreground"
          }`}
        >
          {resultSummary}
        </pre>
      ) : null}
    </div>
  );
}
