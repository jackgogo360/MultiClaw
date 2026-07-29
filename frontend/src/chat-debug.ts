type ChatDebugContext = {
  hostname?: string | null;
};

export function shouldLogChatDebug(context: ChatDebugContext = {}) {
  return context.hostname === "localhost";
}
