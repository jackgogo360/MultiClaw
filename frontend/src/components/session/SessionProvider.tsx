import { useEffect, useCallback, useRef, useSyncExternalStore, type ReactNode } from "react";
import type { UIMessage } from "@ai-sdk/react";
import { sessionApi } from "@/lib/api";
import { planStore } from "@/lib/plan-store";
import { useAuth } from "@/lib/auth-context-store";
import { sessionStore } from "@/lib/session-store";
import { chatStore } from "@/lib/chat-store";
import { SessionContext } from "./session-context";

export function SessionProvider({ children }: { children: ReactNode }) {
  const { userId, accountStatus } = useAuth();
  const state = useSyncExternalStore(
    sessionStore.subscribe,
    sessionStore.getSnapshot,
  );
  const lastResetVersion = useRef(state.resetVersion);
  const authScopeRef = useRef<string>(`${userId ?? "anonymous"}:${accountStatus ?? "none"}`);

  const loadSessions = useCallback(async () => {
    try {
      const list = await sessionApi.list();
      sessionStore.replaceSessions(list);
    } catch {
      // Not authenticated or network error - silently handle
    }
  }, []);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    const nextScope = `${userId ?? "anonymous"}:${accountStatus ?? "none"}`;
    if (authScopeRef.current === nextScope) {
      return;
    }
    authScopeRef.current = nextScope;
    sessionStore.reset();
    chatStore.resetServerState();
    planStore.reset(null);
    if (userId && accountStatus === "active") {
      void loadSessions();
    }
  }, [accountStatus, loadSessions, userId]);

  useEffect(() => {
    if (state.resetVersion === lastResetVersion.current) {
      return;
    }
    lastResetVersion.current = state.resetVersion;
    chatStore.resetServerState();
    planStore.reset(null);
  }, [state.resetVersion]);

  useEffect(() => {
    const sessionId = state.hydrateSessionId;
    if (!sessionId) {
      return;
    }

    // Clear the previous session's Plan snapshot immediately on switch. The
    // network hydration may take a moment (or fail), but stale Plan cards must
    // never remain visible under the newly selected session.
    planStore.reset(sessionId);

    let cancelled = false;

    void (async () => {
      try {
        const [messages, pendingApprovals] = await Promise.all([
          sessionApi.messages(sessionId),
          sessionApi.pendingApprovals(sessionId),
        ]);
        if (cancelled) {
          return;
        }
        await planStore.hydrateSession(sessionId, messages);
        if (cancelled) return;
        const uiMessages: UIMessage[] = messages.map((msg) => ({
          id: msg.id,
          role: msg.role as "user" | "assistant",
          content: msg.content,
          parts: [
            ...(msg.content ? [{ type: "text" as const, text: msg.content }] : []),
            ...(msg.parts ?? []),
          ],
          createdAt: msg.created_at ? new Date(msg.created_at) : new Date(),
        }));
        const approvalMessages: UIMessage[] = pendingApprovals.map((approval) => ({
          id: `approval-${approval.approval_id}`,
          role: "assistant",
          parts: [
            {
              type: "dynamic-tool",
              toolName: approval.tool_name,
              toolCallId: approval.tool_call_id,
              state: "approval-requested",
              input: approval.tool_input,
              approval: { id: approval.approval_id },
            },
          ],
        }));
        chatStore.setMessages([...uiMessages, ...approvalMessages]);
      } catch {
        // Silently handle
      } finally {
        if (!cancelled) {
          sessionStore.finishSessionHydration(sessionId);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [state.hydrateSessionId]);

  const switchSession = useCallback(
    async (id: string) => {
      sessionStore.requestSessionHydration(id);
    },
    []
  );

  const createSession = useCallback(async () => {
    sessionStore.requestNewChat();
  }, []);

  const deleteSession = useCallback(
    async (id: string) => {
      try {
        await sessionApi.del(id);
        sessionStore.removeSession(id);
      } catch {
        // Silently handle
      }
    },
    []
  );

  return (
    <SessionContext.Provider
      value={{
        sessions: state.sessions,
        currentId: state.currentId,
        switchSession,
        createSession,
        deleteSession,
      }}
    >
      {children}
    </SessionContext.Provider>
  );
}
