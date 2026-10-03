import { Thread } from "@/components/assistant-ui/thread";
import { TaskPanel } from "./TaskPanel";
import { CollaborationPanel } from "./CollaborationPanel";
import { useAuth } from "@/lib/auth-context-store";
import { useSyncExternalStore } from "react";
import { sessionStore } from "@/lib/session-store";

export function ChatView({
  chatError,
  requestState,
  onComposerSend,
}: {
  chatError: string | null;
  requestState: "idle" | "sending" | "streaming";
  onComposerSend: () => void;
}) {
  const { userId } = useAuth();
  const { currentId } = useSyncExternalStore(sessionStore.subscribe, sessionStore.getSnapshot);
  return (
    <div className="flex h-full flex-col">
      <TaskPanel key={`${userId}:${currentId}`} />
      <CollaborationPanel />
      <Thread
        chatError={chatError}
        requestState={requestState}
        onComposerSend={onComposerSend}
      />
    </div>
  );
}
