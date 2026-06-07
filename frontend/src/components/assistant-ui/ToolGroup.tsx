import { type PropsWithChildren, useMemo } from "react";
import { useAuiState } from "@assistant-ui/react";

type ToolLikePart = {
  type?: string;
  toolName?: string;
};

function summarizeGroupStatus(statusType: string | undefined): string {
  switch (statusType) {
    case "running":
      return "Running";
    case "requires-action":
      return "Needs approval";
    case "incomplete":
      return "Stopped";
    case "complete":
      return "Done";
    default:
      return "Active";
  }
}

export function ToolGroup({
  startIndex,
  endIndex,
  children,
}: PropsWithChildren<{ startIndex: number; endIndex: number }>) {
  const allParts = useAuiState(
    (state) => state.message.parts as readonly ToolLikePart[]
  );
  const messageStatus = useAuiState((state) => state.message.status?.type);
  const parts = useMemo(
    () => allParts.slice(startIndex, endIndex + 1),
    [allParts, startIndex, endIndex]
  );

  const toolParts = parts.filter((part) => part.type === "tool-call");
  const latestTool =
    (toolParts.length > 0 ? toolParts[toolParts.length - 1]?.toolName : undefined) ?? "tool";
  const summary =
    toolParts.length > 1
      ? `${latestTool} and ${toolParts.length - 1} more`
      : latestTool;

  return (
    <details className="my-2 rounded-xl border border-border/60 bg-elevated/40">
      <summary className="flex cursor-pointer list-none items-center gap-3 px-3 py-2 text-xs text-muted-foreground marker:content-none">
        <span className="inline-flex h-2 w-2 shrink-0 rounded-full bg-accent/80" />
        <span className="font-mono text-foreground/90">{summary}</span>
        <span className="truncate">Tool activity</span>
        <span className="ml-auto shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground/80">
          {summarizeGroupStatus(messageStatus)}
        </span>
      </summary>
      <div className="border-t border-border/50 px-3 py-2">{children}</div>
    </details>
  );
}
