import { Thread } from "@/components/assistant-ui/thread";

export function ChatView({
  chatError,
  requestState,
  onComposerSend,
}: {
  chatError: string | null;
  requestState: "idle" | "sending" | "streaming";
  onComposerSend: () => void;
}) {
  return (
    <div className="flex h-full flex-col">
      <Thread
        chatError={chatError}
        requestState={requestState}
        onComposerSend={onComposerSend}
      />
    </div>
  );
}
