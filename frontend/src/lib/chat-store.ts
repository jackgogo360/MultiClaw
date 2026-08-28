import type { UIMessage } from "ai";

type SetMessagesFn = (messages: UIMessage[]) => void;
type ActiveRun = {
  sessionId: string;
  runId: string;
} | null;

let _setMessages: SetMessagesFn | null = null;
let activeRun: ActiveRun = null;

export const chatStore = {
  register(fn: SetMessagesFn) {
    _setMessages = fn;
  },
  setMessages(messages: UIMessage[]) {
    _setMessages?.(messages);
  },
  setActiveRun(next: ActiveRun) {
    activeRun = next;
  },
  getActiveRun() {
    return activeRun;
  },
  clearActiveRun() {
    activeRun = null;
  },
  resetServerState() {
    activeRun = null;
    _setMessages?.([]);
  },
};
