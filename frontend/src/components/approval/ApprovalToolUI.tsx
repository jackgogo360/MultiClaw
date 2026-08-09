import { useState } from "react";
import { approveApi } from "@/lib/api";

export interface ApprovalToolUIProps {
  approvalId?: string;
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
  status: { type?: string; reason?: string };
  respondToApproval?: (response: { approved: boolean; reason?: string }) => void;
}

export function ApprovalToolUI({
  approvalId,
  toolName,
  args,
  status,
  respondToApproval,
}: ApprovalToolUIProps) {
  const [resolution, setResolution] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleApprove = async () => {
    if (!approvalId) {
      setError("Approval request id is missing.");
      return;
    }

    try {
      const result = await approveApi.submit(approvalId, true);
      if (!result.ok) {
        throw new Error("Backend did not accept the approval request.");
      }
      respondToApproval?.({ approved: true });
      setResolution("approved");
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to approve request.");
    }
  };

  const handleReject = async () => {
    if (!approvalId) {
      setError("Approval request id is missing.");
      return;
    }

    try {
      const result = await approveApi.submit(approvalId, false);
      if (!result.ok) {
        throw new Error("Backend did not accept the approval request.");
      }
      respondToApproval?.({ approved: false });
      setResolution("rejected");
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reject request.");
    }
  };

  if (status.type !== "requires-action") {
    return null; // Use default tool rendering for non-approval tools
  }

  return (
    <div className="my-2 max-w-md rounded-lg border border-accent/20 bg-surface p-4 shadow-sm">
      <div className="mb-2 flex items-start gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-accent/30 bg-accent/10 text-accent">
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
        </div>
        <div className="min-w-0 flex-1">
          <span className="block text-[10px] font-semibold uppercase tracking-wider text-accent/80">
            Approval Required
          </span>
          <span className="font-mono text-sm font-medium tracking-tight">
            {toolName}
          </span>
        </div>
      </div>

      <details className="mb-3 rounded border border-border">
        <summary className="cursor-pointer select-none bg-elevated px-3 py-1.5 text-xs text-muted-foreground">
          Raw params
        </summary>
        <pre className="max-h-[120px] overflow-y-auto bg-background p-3 font-mono text-xs text-muted-foreground whitespace-pre-wrap break-all">
          {JSON.stringify(args, null, 2)}
        </pre>
      </details>

      {error ? (
        <div className="mb-3 rounded-lg border border-danger/25 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </div>
      ) : null}

      {resolution ? (
        <div
          className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium ${
            resolution === "approved"
              ? "border border-success/25 bg-success/10 text-success"
              : "border border-danger/25 bg-danger/10 text-danger"
          }`}
        >
          {resolution === "approved" ? "Approved" : "Rejected"}
        </div>
      ) : (
        <div className="flex gap-2">
          <button
            className="flex-1 rounded-lg bg-accent py-2 text-sm font-medium text-background hover:brightness-110"
            onClick={handleApprove}
          >
            Approve
          </button>
          <button
            className="flex-1 rounded-lg border border-border bg-elevated py-2 text-sm font-medium text-muted-foreground hover:border-danger hover:bg-danger/10 hover:text-danger"
            onClick={handleReject}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}
