import type { UIMessage } from "ai";

type SetMessagesFn = (messages: UIMessage[]) => void;

let _setMessages: SetMessagesFn | null = null;

export const chatStore = {
  register(fn: SetMessagesFn) {
    _setMessages = fn;
  },
  setMessages(messages: UIMessage[]) {
    _setMessages?.(messages);
  },
};
