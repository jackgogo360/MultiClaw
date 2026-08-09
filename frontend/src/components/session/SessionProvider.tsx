import { useEffect, useCallback, useRef, useSyncExternalStore, type ReactNode } from "react";
import { generateId } from "ai";
import type { UIMessage } from "@ai-sdk/react";
import { sessionApi } from "@/lib/api";
import { sessionStore } from "@/lib/session-store";
import { chatStore } from "@/lib/chat-store";
import { SessionContext } from "./session-context";

export function SessionProvider({ children }: { children: ReactNode }) {
  const state = useSyncExternalStore(
    sessionStore.subscribe,
    sessionStore.getSnapshot,
  );
  const lastResetVersion = useRef(state.resetVersion);

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
    if (state.resetVersion === lastResetVersion.current) {
      return;
    }
    lastResetVersion.current = state.resetVersion;
    chatStore.setMessages([]);
  }, [state.resetVersion]);

  useEffect(() => {
    const sessionId = state.hydrateSessionId;
    if (!sessionId) {
      return;
    }

    let cancelled = false;

    void (async () => {
      try {
        const messages = await sessionApi.messages(sessionId);
        if (cancelled) {
          return;
        }
        const uiMessages: UIMessage[] = messages.map((msg) => ({
          id: generateId(),
          role: msg.role as "user" | "assistant",
          content: msg.content,
          parts: [{ type: "text" as const, text: msg.content }],
          createdAt: msg.created_at ? new Date(msg.created_at) : new Date(),
        }));
        chatStore.setMessages(uiMessages);
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
