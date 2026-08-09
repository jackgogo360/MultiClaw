import { type ReactNode, useMemo } from "react";
import {
  ThreadPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  useAuiEvent,
  useAuiState,
} from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import remarkGfm from "remark-gfm";
import { ToolFallback } from "./tool-fallback";

type ToolLikePart = {
  type?: string;
  toolName?: string;
  status?: { type?: string; reason?: string };
};

function ToolGroupBlock({
  indices,
  children,
}: {
  indices: readonly number[];
  children: ReactNode;
}) {
  const parts = useAuiState((s) => s.message.parts) as readonly ToolLikePart[];
  const toolParts = indices
    .map((i) => parts[i])
    .filter((p): p is ToolLikePart => p?.type === "tool-call");

  const toolNames = toolParts.map((p) => p.toolName ?? "tool");
  const running = toolParts.some((p) => p.status?.type === "running");

  const label =
    toolNames.length > 1
      ? `${toolNames[toolNames.length - 1]} 等 ${toolNames.length} 个工具`
      : toolNames[0] ?? "工具";

  return (
    <details className="my-2 rounded-xl border border-border/60 bg-elevated/40" open={running}>
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs text-muted-foreground marker:content-none">
        <span className="inline-flex h-2 w-2 shrink-0 rounded-full bg-accent/80" />
        <span className="font-mono text-foreground/90">{label}</span>
        <span className="ml-auto shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground/80">
          {running ? "Running" : "Done"}
        </span>
      </summary>
      <div className="border-t border-border/50 px-3 py-2">{children}</div>
    </details>
  );
}

type PartLike = { type?: string };

function groupAllTools(
  parts: readonly PartLike[],
): { groupKey: string | undefined; indices: number[] }[] {
  const toolIndices: number[] = [];
  const otherGroups: { groupKey: string | undefined; indices: number[] }[] = [];

  for (let i = 0; i < parts.length; i++) {
    if (parts[i].type === "tool-call") {
      toolIndices.push(i);
    } else {
      otherGroups.push({ groupKey: undefined, indices: [i] });
    }
  }

  if (toolIndices.length === 0) return otherGroups;

  return [
    { groupKey: "all-tools", indices: toolIndices },
    ...otherGroups,
  ];
}

function LoadingDots() {
  return (
    <div className="flex items-center gap-1.5 py-2">
      <span
        className="inline-block h-1.5 w-1.5 rounded-full bg-muted-foreground/60"
        style={{ animation: "blink-dot 1.4s ease-in-out infinite", animationDelay: "0ms" }}
      />
      <span
        className="inline-block h-1.5 w-1.5 rounded-full bg-muted-foreground/60"
        style={{ animation: "blink-dot 1.4s ease-in-out infinite", animationDelay: "200ms" }}
      />
      <span
        className="inline-block h-1.5 w-1.5 rounded-full bg-muted-foreground/60"
        style={{ animation: "blink-dot 1.4s ease-in-out infinite", animationDelay: "400ms" }}
      />
    </div>
  );
}

function AssistantBubble() {
  const parts = useAuiState((s) => s.message.parts) as readonly PartLike[];
  const isRunning = useAuiState((s) => s.thread.isRunning);

  const groupingFunction = useMemo(() => groupAllTools, []);

  if (parts.length === 0 && isRunning) {
    return <LoadingDots />;
  }

  return (
    <MessagePrimitive.Unstable_PartsGrouped
      groupingFunction={groupingFunction}
      components={{
        Text: () => <MarkdownTextPrimitive className="aui-md" smooth remarkPlugins={[remarkGfm]} />,
        tools: {
          Override: ToolFallback,
        },
        Group: ({ groupKey, indices, children }) => {
          if (groupKey === "all-tools") {
            return (
              <ToolGroupBlock indices={indices}>
                {children}
              </ToolGroupBlock>
            );
          }
          return <>{children}</>;
        },
      }}
    />
  );
}

function ChatStatusBar({
  chatError,
  requestState,
}: {
  chatError: string | null;
  requestState: "idle" | "sending" | "streaming";
}) {
  const isRunning = useAuiState((state) => state.thread.isRunning);

  if (chatError) {
    return (
      <div className="border-b border-danger/20 bg-danger/10 px-4 py-2 text-sm text-danger">
        Request failed: {chatError}
      </div>
    );
  }

  if (requestState === "sending") {
    return (
      <div className="border-b border-border bg-elevated px-4 py-2 text-sm text-muted-foreground">
        Sending request to backend...
      </div>
    );
  }

  if (requestState === "streaming" || isRunning) {
    return (
      <div className="border-b border-border bg-elevated px-4 py-2 text-sm text-muted-foreground">
        Backend accepted the request. Waiting for model output...
      </div>
    );
  }

  return null;
}

function SendButton({ requestState }: { requestState: "idle" | "sending" | "streaming" }) {
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const label =
    requestState === "sending"
      ? "Sending..."
      : requestState === "streaming" || isRunning
        ? "Thinking..."
        : "Send";

  return (
    <>{label}</>
  );
}

export function Thread({
  chatError,
  requestState,
  onComposerSend,
}: {
  chatError: string | null;
  requestState: "idle" | "sending" | "streaming";
  onComposerSend: () => void;
}) {
  useAuiEvent("composer.send", onComposerSend);

  return (
    <ThreadPrimitive.Root className="flex h-full flex-col bg-surface text-foreground">
      <ChatStatusBar chatError={chatError} requestState={requestState} />
      <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto px-4 py-6">
        <ThreadPrimitive.Empty>
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
            <p className="text-lg">Start a conversation</p>
            <p className="text-sm mt-2">Type a message below to begin</p>
          </div>
        </ThreadPrimitive.Empty>

        {requestState === "sending" && (
          <div className="flex justify-start mb-4">
            <div className="bg-elevated text-foreground rounded-2xl px-4 py-3 shadow-sm">
              <LoadingDots />
            </div>
          </div>
        )}

        <ThreadPrimitive.Messages>
          {({ message }) => {
            const isUser = message.role === "user";
            const bubbleClassName = isUser
              ? "bg-accent text-background"
              : "bg-elevated text-foreground";

            return (
              <MessagePrimitive.Root
                className={`mb-4 flex ${isUser ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[min(80ch,85%)] rounded-2xl px-4 py-3 shadow-sm ${bubbleClassName}`}
                >
                  {isUser ? (
                    <MessagePrimitive.Parts />
                  ) : (
                    <AssistantBubble />
                  )}
                </div>
              </MessagePrimitive.Root>
            );
          }}
        </ThreadPrimitive.Messages>

        <ThreadPrimitive.ScrollToBottom className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 flex items-center gap-1.5 rounded-full bg-elevated border border-border px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors shadow-lg">
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
          Scroll to bottom
        </ThreadPrimitive.ScrollToBottom>
      </ThreadPrimitive.Viewport>

      <ComposerPrimitive.Root className="border-t border-border bg-surface p-4">
        <ComposerPrimitive.Input
          autoFocus
          placeholder="Type a message..."
          className="w-full resize-none rounded-xl border border-border bg-input px-4 py-3 text-foreground placeholder:text-muted-foreground outline-none focus:border-accent transition-colors"
          rows={1}
        />
        <div className="flex items-center justify-between mt-3">
          <div className="flex items-center gap-2" />
          <div className="flex items-center gap-2">
            <ComposerPrimitive.Cancel className="flex items-center gap-1.5 rounded-lg border border-danger/30 px-3 py-1.5 text-sm text-danger hover:bg-danger/10 transition-colors">
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="currentColor"
              >
                <rect x="4" y="4" width="16" height="16" rx="2" />
              </svg>
              Stop
            </ComposerPrimitive.Cancel>
            <ComposerPrimitive.Send className="flex items-center gap-1.5 rounded-xl bg-accent px-4 py-2 text-sm font-medium text-background hover:brightness-110 transition-all">
              <SendButton requestState={requestState} />
            </ComposerPrimitive.Send>
          </div>
        </div>
      </ComposerPrimitive.Root>
    </ThreadPrimitive.Root>
  );
}
