import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context-store";

function formatError(error: unknown) {
  if (error instanceof ApiError) {
    return error.message;
  }
  return error instanceof Error ? error.message : "Request failed.";
}

export function DeletionSettings({
  standalone = false,
  onBack,
}: {
  standalone?: boolean;
  onBack?: () => void;
}) {
  const {
    email,
    accountStatus,
    deletionStatus,
    pendingDeletionEmail,
    requestAccountDeletion,
    sendDeletionRecoveryCode,
    verifyDeletionRecoveryCode,
    refreshDeletionStatus,
    recoverAccountDeletion,
  } = useAuth();
  const [confirmEmail, setConfirmEmail] = useState(email ?? "");
  const [recoveryEmail, setRecoveryEmail] = useState(
    pendingDeletionEmail ?? email ?? "",
  );
  const [recoveryCode, setRecoveryCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [currentTime, setCurrentTime] = useState(() => Date.now());

  const pendingPurge = accountStatus === "pending_purge";
  const purgeAfter = deletionStatus?.purge_after ?? null;
  const shouldTrackPurgeDeadline =
    pendingPurge && deletionStatus?.status === "scheduled" && purgeAfter !== null;

  useEffect(() => {
    if (!shouldTrackPurgeDeadline || purgeAfter === null) {
      return;
    }
    const remainingMs = purgeAfter - currentTime;
    if (remainingMs <= 0) {
      return;
    }
    const timer = window.setTimeout(() => {
      setCurrentTime(Date.now());
    }, Math.min(remainingMs, 1_000));
    return () => window.clearTimeout(timer);
  }, [currentTime, purgeAfter, shouldTrackPurgeDeadline]);

  const canRecover =
    pendingPurge &&
    deletionStatus?.status === "scheduled" &&
    purgeAfter !== null &&
    purgeAfter > currentTime;

  const handleDeletionRequest = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const scheduled = await requestAccountDeletion();
      setMessage(
        `Deletion scheduled. Recover before ${new Date(scheduled.purge_after).toLocaleString()}.`,
      );
    } catch (nextError) {
      setError(formatError(nextError));
    } finally {
      setBusy(false);
    }
  };

  const handleSendRecoveryCode = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await sendDeletionRecoveryCode(recoveryEmail);
      setMessage("Recovery code sent if a scheduled deletion exists for this email.");
    } catch (nextError) {
      setError(formatError(nextError));
    } finally {
      setBusy(false);
    }
  };

  const handleVerifyRecoveryCode = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await verifyDeletionRecoveryCode(recoveryEmail, recoveryCode);
      const status = await refreshDeletionStatus();
      if (status?.purge_after) {
        setMessage(
          `Recovery unlocked until ${new Date(status.purge_after).toLocaleString()}.`,
        );
      } else {
        setMessage("Recovery unlocked.");
      }
    } catch (nextError) {
      setError(formatError(nextError));
    } finally {
      setBusy(false);
    }
  };

  const handleRefreshStatus = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const status = await refreshDeletionStatus();
      if (status?.purge_after) {
        setMessage(
          `Server status: ${status.status} until ${new Date(status.purge_after).toLocaleString()}.`,
        );
      } else {
        setMessage("No active scheduled deletion was found.");
      }
    } catch (nextError) {
      setError(formatError(nextError));
    } finally {
      setBusy(false);
    }
  };

  const handleRecover = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await recoverAccountDeletion();
      setMessage("Account recovered. Sign in again to continue.");
      onBack?.();
    } catch (nextError) {
      setError(formatError(nextError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="space-y-4">
      <div className="space-y-1">
        <h3 className="text-sm font-semibold text-foreground">Account deletion</h3>
        <p className="text-sm text-muted-foreground">
          Scheduled deletion moves the account to <code>pending_purge</code>, signs out normal
          access, and keeps a recovery window open until the server&apos;s <code>purge_after</code>
          timestamp.
        </p>
      </div>

      {!pendingPurge && !standalone ? (
        <div className="space-y-3 rounded-xl border border-border bg-background/70 p-4">
          <p className="text-sm text-muted-foreground">
            Enter your email to confirm deletion. Recovery later requires an email-code confirmation.
          </p>
          <label className="space-y-1 text-sm">
            <span className="text-muted-foreground">Confirm email</span>
            <input
              className="w-full rounded-lg border border-border bg-input px-3 py-2 text-sm"
              value={confirmEmail}
              onChange={(event) => setConfirmEmail(event.target.value)}
              placeholder={email ?? "you@example.com"}
            />
          </label>
          <button
            className="rounded-lg bg-danger px-4 py-2 text-sm font-medium text-white hover:brightness-110 disabled:opacity-50"
            onClick={handleDeletionRequest}
            disabled={busy || !email || confirmEmail.trim().toLowerCase() !== email.toLowerCase()}
          >
            Schedule deletion
          </button>
        </div>
      ) : null}

      {(pendingPurge || standalone) && (
        <div className="deletion-warning">
          <div className="space-y-1">
            <div className="text-sm font-medium text-foreground">Recovery</div>
            <p className={canRecover ? "pending-purge-banner" : "text-sm text-muted-foreground"}>
              {canRecover && purgeAfter
                ? `Server status is scheduled. Recover before ${new Date(purgeAfter).toLocaleString()}.`
                : "Use your email and a recovery code to check status or restore access before the purge window closes."}
            </p>
          </div>

          <label className="space-y-1 text-sm">
            <span className="text-muted-foreground">Email</span>
            <input
              className="w-full rounded-lg border border-border bg-input px-3 py-2 text-sm"
              value={recoveryEmail}
              onChange={(event) => setRecoveryEmail(event.target.value)}
              placeholder="you@example.com"
            />
          </label>

          <div className="flex flex-wrap gap-2">
            <button
              className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:border-accent hover:text-accent disabled:opacity-50"
              onClick={handleSendRecoveryCode}
              disabled={busy || !recoveryEmail}
            >
              Send recovery code
            </button>
            <button
              className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:border-accent hover:text-accent disabled:opacity-50"
              onClick={handleRefreshStatus}
              disabled={busy}
            >
              Refresh server status
            </button>
          </div>

          <label className="space-y-1 text-sm">
            <span className="text-muted-foreground">Recovery code</span>
            <input
              className="w-full rounded-lg border border-border bg-input px-3 py-2 font-mono text-sm tracking-[0.2em]"
              value={recoveryCode}
              onChange={(event) => setRecoveryCode(event.target.value)}
              placeholder="123456"
              inputMode="numeric"
            />
          </label>

          <div className="flex flex-wrap gap-2">
            <button
              className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-background hover:brightness-110 disabled:opacity-50"
              onClick={handleVerifyRecoveryCode}
              disabled={busy || recoveryCode.trim().length !== 6 || !recoveryEmail}
            >
              Verify code
            </button>
            <button
              className="rounded-lg bg-success px-4 py-2 text-sm font-medium text-background hover:brightness-110 disabled:opacity-50"
              onClick={handleRecover}
              disabled={busy || !canRecover}
            >
              Recover account
            </button>
            {standalone && onBack ? (
              <button
                className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:border-accent hover:text-accent"
                onClick={onBack}
                disabled={busy}
              >
                Back
              </button>
            ) : null}
          </div>
        </div>
      )}

      {error ? (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </div>
      ) : null}
      {message ? (
        <div className="rounded-lg border border-success/30 bg-success/10 px-3 py-2 text-sm text-success">
          {message}
        </div>
      ) : null}
    </section>
  );
}
