import { createContext, useContext } from "react";
import type { AccountDeletionRequest, AccountDeletionStatus } from "./api";

export interface AuthState {
  email: string | null;
  userId: string | null;
  accountStatus: "active" | "pending_purge" | null;
  deletionStatus: AccountDeletionStatus | null;
  pendingDeletionEmail: string | null;
  reauthEmailHint: string | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  login: (email: string, code: string) => Promise<void>;
  sendCode: (email: string) => Promise<void>;
  sendDeletionRecoveryCode: (email: string) => Promise<void>;
  verifyDeletionRecoveryCode: (email: string, code: string) => Promise<void>;
  requestAccountDeletion: () => Promise<AccountDeletionRequest>;
  refreshDeletionStatus: () => Promise<AccountDeletionStatus | null>;
  recoverAccountDeletion: () => Promise<void>;
  beginRecentAuthRenewal: () => Promise<void>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthState | null>(null);

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
