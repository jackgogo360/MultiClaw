import type { Session } from "./api";

type SessionState = {
  currentId: string | null;
  sessions: Session[];
  hydrateSessionId: string | null;
  resetVersion: number;
};

type SessionPatch = Pick<Session, "id" | "title"> & Partial<Session>;

const listeners = new Set<() => void>();

let state: SessionState = {
  currentId: null,
  sessions: [],
  hydrateSessionId: null,
  resetVersion: 0,
};

function emit() {
  for (const listener of listeners) {
    listener();
  }
}

function setState(nextState: SessionState) {
  state = nextState;
  emit();
}

function normalizeSession(session: SessionPatch): Session {
  const now = new Date().toISOString();
  return {
    status: "active",
    created_at: now,
    updated_at: now,
    ...session,
  };
}

export const sessionStore = {
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },

  getSnapshot() {
    return state;
  },

  reset() {
    setState({
      currentId: null,
      sessions: [],
      hydrateSessionId: null,
      resetVersion: state.resetVersion + 1,
    });
  },

  replaceSessions(sessions: Session[]) {
    const currentId =
      state.currentId && sessions.some((session) => session.id === state.currentId)
        ? state.currentId
        : null;
    setState({ ...state, sessions, currentId });
  },

  syncSession(session: SessionPatch) {
    const normalized = normalizeSession(session);
    const sessions = [
      normalized,
      ...state.sessions.filter((existing) => existing.id !== normalized.id),
    ];
    setState({ ...state, currentId: normalized.id, sessions });
  },

  requestSessionHydration(sessionId: string) {
    setState({ ...state, currentId: sessionId, hydrateSessionId: sessionId });
  },

  finishSessionHydration(sessionId: string) {
    if (state.hydrateSessionId !== sessionId) return;
    setState({ ...state, hydrateSessionId: null });
  },

  requestNewChat() {
    setState({
      ...state,
      currentId: null,
      hydrateSessionId: null,
      resetVersion: state.resetVersion + 1,
    });
  },

  removeSession(sessionId: string) {
    const sessions = state.sessions.filter((session) => session.id !== sessionId);
    const currentId = state.currentId === sessionId ? null : state.currentId;
    setState({
      ...state,
      sessions,
      currentId,
      hydrateSessionId: state.hydrateSessionId === sessionId ? null : state.hydrateSessionId,
      resetVersion:
        state.currentId === sessionId ? state.resetVersion + 1 : state.resetVersion,
    });
  },
};
