type MessageContentPart = {
  type?: string;
  text?: string;
};

type ChatMessage = {
  role?: string;
  content?: string | MessageContentPart[];
  parts?: MessageContentPart[];
};

export function extractLatestUserText(messages: ChatMessage[]): string {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== "user") continue;

    if (typeof message.content === "string" && message.content.trim()) {
      return message.content;
    }

    if (Array.isArray(message.content)) {
      const text = extractTextParts(message.content);
      if (text.trim()) {
        return text;
      }
    }

    if (Array.isArray(message.parts)) {
      const text = extractTextParts(message.parts);
      if (text.trim()) {
        return text;
      }
    }
  }

  throw new Error("No user message found in request");
}

function extractTextParts(parts: MessageContentPart[]): string {
  return parts
    .filter((part) => part.type === "text" && typeof part.text === "string")
    .map((part) => part.text ?? "")
    .join("");
}
