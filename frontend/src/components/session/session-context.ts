import { createContext, useContext } from "react";
import type { Session } from "@/lib/api";

export interface SessionContextValue {
  sessions: Session[];
  currentId: string | null;
  switchSession: (id: string) => Promise<void>;
  createSession: () => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
}

export const SessionContext = createContext<SessionContextValue | null>(null);

export function useSessions() {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSessions must be used within SessionProvider");
  return ctx;
}
