import { useEffect, useState } from "react";
import { ApiError, approveApi, type ApprovalRecord } from "@/lib/api";

export interface ApprovalToolUIProps {
  approvalId?: string;
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
  status: { type?: string; reason?: string };
  respondToApproval?: (response: { approved: boolean; reason?: string }) => void;
}

function formatError(error: unknown) {
  if (error instanceof ApiError) {
    return error.message;
  }
  return error instanceof Error ? error.message : "Failed to resolve approval.";
}

export function ApprovalToolUI({
  approvalId,
  toolName,
  args,
  status,
  respondToApproval,
}: ApprovalToolUIProps) {
  const [approval, setApproval] = useState<ApprovalRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isReloading, setIsReloading] = useState(false);

  const loadApproval = async (): Promise<ApprovalRecord> => {
    if (!approvalId) {
      throw new Error("Approval request id is missing.");
    }
    return approveApi.getApproval(approvalId);
  };

  const handleReloadApproval = async () => {
    if (!approvalId) {
      setError("Approval request id is missing.");
      return;
    }

    setIsReloading(true);
    try {
      const next = await loadApproval();
      setApproval(next);
      setError(null);
    } catch (nextError) {
      setError(formatError(nextError));
    } finally {
      setIsReloading(false);
    }
  };

  useEffect(() => {
    if (!approvalId) {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const next = await approveApi.getApproval(approvalId);
        if (!cancelled) {
          setApproval(next);
          setError(null);
        }
      } catch (nextError) {
        if (!cancelled) {
          setError(formatError(nextError));
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [approvalId]);

  const submitDecision = async (approved: boolean) => {
    if (!approvalId) {
      setError("Approval request id is missing.");
      return;
    }

    setBusy(true);
    setError(null);
    let currentApproval = approval;
    try {
      currentApproval ??= await loadApproval();
      const result = await approveApi.submit(
        approvalId,
        approved,
        currentApproval.version,
      );
      setApproval(result);
      respondToApproval?.({ approved });
    } catch (nextError) {
      if (nextError instanceof ApiError && nextError.status === 409) {
        try {
          const refreshed = await loadApproval();
          setApproval(refreshed);
          setError("Approval state changed on the server. Refreshed latest status.");
        } catch (refreshError) {
          setError(formatError(refreshError));
        }
      } else if (nextError instanceof ApiError && nextError.status === 410) {
        setApproval(
          currentApproval ? { ...currentApproval, status: "expired" } : null,
        );
        setError("This approval expired before your decision could be recorded.");
      } else {
        setError(formatError(nextError));
      }
    } finally {
      setBusy(false);
    }
  };

  if (status.type !== "requires-action") {
    return null;
  }

  const resolved = approval?.status === "approved" || approval?.status === "rejected";
  const expired = approval?.status === "expired";

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
          {approval ? (
            <div className="mt-1 text-xs text-muted-foreground">
              Status: {approval.status} · version {approval.version}
            </div>
          ) : null}
        </div>
        <button
          className="approval-refresh-button"
          onClick={() => void handleReloadApproval()}
          disabled={busy || isReloading || !approvalId}
        >
          {isReloading ? "Reloading..." : "Reload status"}
        </button>
      </div>

      <details className="mb-3 rounded border border-border">
        <summary className="cursor-pointer select-none bg-elevated px-3 py-1.5 text-xs text-muted-foreground">
          Raw params
        </summary>
        <pre className="max-h-[120px] overflow-y-auto break-all whitespace-pre-wrap bg-background p-3 font-mono text-xs text-muted-foreground">
          {JSON.stringify(args, null, 2)}
        </pre>
      </details>

      {error ? (
        <div className="mb-3 rounded-lg border border-danger/25 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </div>
      ) : null}

      {resolved || expired ? (
        <div
          className={
            approval?.status === "approved"
              ? "approval-state approval-state--approved"
              : expired
                ? "approval-state approval-state--expired"
                : "approval-state approval-state--rejected"
          }
        >
          {approval?.status === "approved"
            ? "Approved"
            : approval?.status === "rejected"
              ? "Rejected"
              : "Expired"}
        </div>
      ) : (
        <div className="flex gap-2">
          <button
            className="flex-1 rounded-lg bg-accent py-2 text-sm font-medium text-background hover:brightness-110 disabled:opacity-50"
            onClick={() => void submitDecision(true)}
            disabled={busy}
          >
            Approve
          </button>
          <button
            className="flex-1 rounded-lg border border-border bg-elevated py-2 text-sm font-medium text-muted-foreground hover:border-danger hover:bg-danger/10 hover:text-danger disabled:opacity-50"
            onClick={() => void submitDecision(false)}
            disabled={busy}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}
