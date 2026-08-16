import { useEffect, useMemo, useRef, useState } from "react";
import { AssistantRuntimeProvider } from "@assistant-ui/react";
import {
  useAISDKRuntime,
  AssistantChatTransport,
} from "@assistant-ui/react-ai-sdk";
import {
  useRemoteThreadListRuntime,
  useCloudThreadListAdapter,
} from "@assistant-ui/core/react";
import { useAuiState, useAui } from "@assistant-ui/store";
import { useChat, type UIMessage } from "@ai-sdk/react";
import { AuthProvider } from "@/lib/auth-context";
import { useAuth } from "@/lib/auth-context-store";
import { LoginOverlay } from "@/components/login/LoginOverlay";
import { AppLayout } from "@/components/layout/AppLayout";
import { ChatView } from "@/components/chat/ChatView";
import { API_BASE } from "@/lib/constants";
import { SessionProvider } from "@/components/session/SessionProvider";
import { SessionList } from "@/components/session/SessionList";
import { DeletionSettings } from "@/components/settings/DeletionSettings";
import { shouldLogChatDebug } from "@/chat-debug";
import { extractLatestUserText } from "@/lib/chat-request";
import { sessionStore } from "@/lib/session-store";
import { chatStore } from "@/lib/chat-store";
import { ensureCsrfToken } from "@/lib/security";

type ChatRequestState = "idle" | "sending" | "streaming";
type ActiveRun = {
  sessionId: string;
  runId: string;
};
type DataPart = {
  type: string;
  data?: unknown;
};

type LatestTransportController = {
  proxy: AssistantChatTransport<UIMessage>;
  update: (transport: AssistantChatTransport<UIMessage> | undefined) => void;
};

function createLatestTransportProxy(
  initialTransport: AssistantChatTransport<UIMessage> | undefined,
): LatestTransportController {
  let currentTransport = initialTransport;
  const proxy = new Proxy({} as AssistantChatTransport<UIMessage>, {
    get(_, prop) {
      const current = currentTransport;
      const res = current?.[prop as keyof typeof current];
      return typeof res === "function" ? res.bind(current) : res;
    },
  }) as unknown as AssistantChatTransport<UIMessage>;

  return {
    proxy,
    update(transport) {
      currentTransport = transport;
    },
  };
}

function readRunScope(data: unknown): ActiveRun | null {
  if (!data || typeof data !== "object") {
    return null;
  }
  const record = data as { session_id?: unknown; run_id?: unknown };
  if (typeof record.session_id !== "string" || typeof record.run_id !== "string") {
    return null;
  }
  return { sessionId: record.session_id, runId: record.run_id };
}

function shouldIgnoreScopedEvent(part: DataPart) {
  const scoped = readRunScope(part.data);
  if (!scoped) {
    return false;
  }
  const activeRun = chatStore.getActiveRun();
  if (!activeRun) {
    return part.type !== "data-run";
  }
  return activeRun.sessionId !== scoped.sessionId || activeRun.runId !== scoped.runId;
}

function RecoveryScreen({ onBack }: { onBack: () => void }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4 py-10">
      <div className="w-full max-w-2xl rounded-2xl border border-border bg-surface p-6 shadow-2xl">
        <DeletionSettings standalone onBack={onBack} />
      </div>
    </div>
  );
}

/**
 * Custom wrapper around useChat + useAISDKRuntime + useRemoteThreadListRuntime
 * that exposes chat.setMessages through chatStore so SessionProvider
 * can load historical messages without going through thread.reset()
 * (which loses AI SDK UIMessage bindings).
 */
function useChatRuntimeWithStore(
  options: Record<string, unknown>,
): ReturnType<typeof useRemoteThreadListRuntime> {
  const { cloud, ...rest } = options;
  const cloudAdapter = useCloudThreadListAdapter({
    cloud: cloud as Parameters<typeof useCloudThreadListAdapter>[0]["cloud"],
  });

  return useRemoteThreadListRuntime({
    runtimeHook: function RuntimeHook() {
      return useChatThreadRuntimeWithStore(rest);
    },
    adapter: cloudAdapter,
    allowNesting: true,
  });
}

function useChatThreadRuntimeWithStore(options: Record<string, unknown>) {
  const transportOptions = options.transport as
    | AssistantChatTransport<UIMessage>
    | undefined;
  const [transportController] = useState(() =>
    createLatestTransportProxy(transportOptions),
  );
  useEffect(() => {
    transportController.update(transportOptions);
  }, [transportController, transportOptions]);
  const transport = transportController.proxy;

  const id = useAuiState((s) => s.threadListItem.id);
  const aui = useAui();
  const chat = useChat<UIMessage>({
    id,
    transport: transport as never,
  });

  useEffect(() => {
    chatStore.register(chat.setMessages);
  }, [chat.setMessages]);

  const runtimeOptions: Record<string, unknown> = {};
  if (options.adapters != null) runtimeOptions.adapters = options.adapters;
  if (options.toCreateMessage != null) runtimeOptions.toCreateMessage = options.toCreateMessage;
  if (options.onResume != null) runtimeOptions.onResume = options.onResume;
  if (options.suggestions != null) runtimeOptions.suggestions = options.suggestions;
  const runtime = useAISDKRuntime(chat as Parameters<typeof useAISDKRuntime>[0], runtimeOptions as Parameters<typeof useAISDKRuntime>[1]);

  if (transportOptions instanceof AssistantChatTransport) {
    transportOptions.setRuntime(runtime);
    transportOptions.__internal_setGetThreadListItem(() =>
      aui.threadListItem.source ? aui.threadListItem() : undefined,
    );
  }

  const resumeFiredRef = useRef(false);
  useEffect(() => {
    if (resumeFiredRef.current) return;
    if (!(transportOptions instanceof AssistantChatTransport)) return;
    const adapter = transportOptions.getResumableAdapter();
    if (!adapter) return;
    const pending = adapter.storage.getStreamId();
    if (!pending) return;
    resumeFiredRef.current = true;
    chat.resumeStream?.().catch((err: unknown) => {
      console.warn("[assistant-ui] resumable: resume failed", err);
      adapter.storage.clear();
    });
  }, [transportOptions, chat]);

  return runtime;
}

function ChatApp() {
  const { isAuthenticated, isLoading, accountStatus } = useAuth();
  const [chatError, setChatError] = useState<string | null>(null);
  const [requestState, setRequestState] = useState<ChatRequestState>("idle");
  const [showRecovery, setShowRecovery] = useState(false);
  const recoveryMode = showRecovery || accountStatus === "pending_purge";

  const transport = useMemo(
    () =>
      new AssistantChatTransport({
        api: `${API_BASE}/chat`,
        credentials: "include",
        prepareSendMessagesRequest: ({ messages }) => {
          try {
            const latestMessage = extractLatestUserText(
              messages as Parameters<typeof extractLatestUserText>[0]
            );
            const sessionId = sessionStore.getSnapshot().currentId ?? undefined;
            chatStore.clearActiveRun();

            if (shouldLogChatDebug({ hostname: window.location.hostname })) {
              console.debug("[chat] prepare request", {
                sessionId,
                messageCount: messages.length,
                preview: latestMessage.slice(0, 120),
              });
            }

            return {
              body: {
                message: latestMessage,
                session_id: sessionId,
              },
            };
          } catch (error) {
            const message =
              error instanceof Error ? error.message : "Failed to prepare chat request.";
            if (shouldLogChatDebug({ hostname: window.location.hostname })) {
              console.error("[chat] prepare request failed", error, messages);
            }
            setRequestState("idle");
            setChatError(message);
            throw error;
          }
        },
        fetch: async (input, init) => {
          if (shouldLogChatDebug({ hostname: window.location.hostname })) {
            console.debug("[chat] fetch start", input, init);
          }
          const baseHeaders = input instanceof Request ? input.headers : undefined;
          const headers = new Headers(baseHeaders);
          for (const [key, value] of new Headers(init?.headers).entries()) {
            headers.set(key, value);
          }
          headers.set("X-CSRF-Token", await ensureCsrfToken());
          const response = await fetch(input, {
            ...init,
            credentials: "include",
            headers,
          });
          if (shouldLogChatDebug({ hostname: window.location.hostname })) {
            console.debug("[chat] fetch response", response.status, response.url);
          }
          return response;
        },
      }),
    []
  );

  const runtime = useChatRuntimeWithStore({
    transport,
    onData: (part: DataPart) => {
      if (part.type === "data-run") {
        const scope = readRunScope(part.data);
        if (scope) {
          chatStore.setActiveRun(scope);
        }
        return;
      }

      if (shouldIgnoreScopedEvent(part)) {
        return;
      }

      setRequestState("streaming");
      if (part.type !== "data-session") {
        return;
      }
      const data = part.data as Partial<{
        id: string;
        title: string;
        status: "active" | "archived";
        created_at: string;
        updated_at: string;
      }>;
      if (!data.id || !data.title) {
        return;
      }
      sessionStore.syncSession({
        id: data.id,
        title: data.title,
        status: data.status,
        created_at: data.created_at,
        updated_at: data.updated_at,
      });
    },
    onError: (error: Error) => {
      chatStore.clearActiveRun();
      setRequestState("idle");
      setChatError(error.message);
    },
    onFinish: () => {
      chatStore.clearActiveRun();
      setRequestState("idle");
      setChatError(null);
    },
  });

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-background text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (!isAuthenticated) {
    if (recoveryMode) {
      return <RecoveryScreen onBack={() => setShowRecovery(false)} />;
    }
    return (
      <>
        <LoginOverlay />
        <button
          className="fixed right-4 bottom-4 z-[60] rounded-full border border-border bg-surface px-4 py-2 text-sm text-muted-foreground shadow-lg hover:border-accent hover:text-accent"
          onClick={() => setShowRecovery(true)}
        >
          Recover scheduled deletion
        </button>
      </>
    );
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <SessionProvider>
        <AppLayout sidebar={<SessionList />} navigationDisabled={accountStatus === "pending_purge"}>
          <ChatView
            chatError={chatError}
            requestState={requestState}
            onComposerSend={() => {
              chatStore.clearActiveRun();
              setRequestState("sending");
              setChatError(null);
            }}
          />
        </AppLayout>
      </SessionProvider>
    </AssistantRuntimeProvider>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <ChatApp />
    </AuthProvider>
  );
}
