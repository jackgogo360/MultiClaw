import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  accountApi,
  authApi,
  type AccountDeletionRequest,
  type AccountDeletionStatus,
} from "./api";
import { AuthContext } from "./auth-context-store";
import { chatStore } from "./chat-store";
import { clearCsrfToken, invalidateCsrfToken } from "./security";
import { sessionStore } from "./session-store";

function resetServerState() {
  sessionStore.reset();
  chatStore.resetServerState();
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [email, setEmail] = useState<string | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [accountStatus, setAccountStatus] = useState<"active" | "pending_purge" | null>(null);
  const [pendingDeletionEmail, setPendingDeletionEmail] = useState<string | null>(null);
  const [deletionStatus, setDeletionStatus] = useState<AccountDeletionStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const setSignedOut = (nextPendingEmail: string | null = null) => {
    setEmail(null);
    setUserId(null);
    setPendingDeletionEmail(nextPendingEmail);
  };

  const refreshDeletionStatus = async () => {
    try {
      const status = await accountApi.status();
      setAccountStatus("pending_purge");
      setDeletionStatus(status);
      return status;
    } catch {
      setDeletionStatus(null);
      return null;
    }
  };

  useEffect(() => {
    let cancelled = false;

    void (async () => {
      try {
        const data = await authApi.me();
        if (cancelled) {
          return;
        }
        if (data.user_id) {
          setEmail(data.email ?? null);
          setUserId(data.user_id);
          setAccountStatus("active");
          setDeletionStatus(null);
          setPendingDeletionEmail(null);
          return;
        }

        const status = await accountApi.status().catch(() => null);
        if (cancelled) {
          return;
        }
        if (status) {
          setAccountStatus("pending_purge");
          setDeletionStatus(status);
        } else {
          setAccountStatus(null);
          setDeletionStatus(null);
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const value = useMemo(
    () => ({
      email,
      userId,
      accountStatus,
      deletionStatus,
      pendingDeletionEmail,
      isLoading,
      isAuthenticated: email !== null && userId !== null,
      sendCode: async (emailAddr: string) => {
        await authApi.sendCode(emailAddr);
      },
      login: async (emailAddr: string, code: string) => {
        await authApi.verify(emailAddr, code);
        await invalidateCsrfToken();
        const data = await authApi.me();
        setEmail(data.email ?? emailAddr);
        setUserId(data.user_id ?? null);
        setAccountStatus(data.user_id ? "active" : null);
        setDeletionStatus(null);
        setPendingDeletionEmail(null);
        resetServerState();
      },
      sendDeletionRecoveryCode: async (emailAddr: string) => {
        await authApi.sendDeletionRecoveryCode(emailAddr);
        setPendingDeletionEmail(emailAddr);
      },
      verifyDeletionRecoveryCode: async (emailAddr: string, code: string) => {
        await authApi.verifyDeletionRecoveryCode(emailAddr, code);
        await invalidateCsrfToken();
        setSignedOut(emailAddr);
        setAccountStatus("pending_purge");
        await refreshDeletionStatus();
        resetServerState();
      },
      requestAccountDeletion: async () => {
        const scheduled: AccountDeletionRequest = await accountApi.requestDeletion();
        clearCsrfToken();
        setSignedOut(email);
        setAccountStatus("pending_purge");
        setDeletionStatus({
          status: scheduled.status,
          purge_after: scheduled.purge_after,
        });
        resetServerState();
        return scheduled;
      },
      refreshDeletionStatus: async () => refreshDeletionStatus(),
      recoverAccountDeletion: async () => {
        await accountApi.recover();
        clearCsrfToken();
        setSignedOut(null);
        setAccountStatus(null);
        setDeletionStatus(null);
        resetServerState();
      },
      logout: async () => {
        await authApi.logout();
        clearCsrfToken();
        setSignedOut(null);
        setAccountStatus(null);
        setDeletionStatus(null);
        resetServerState();
      },
    }),
    [accountStatus, deletionStatus, email, isLoading, pendingDeletionEmail, userId]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
