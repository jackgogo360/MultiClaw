import { useMemo, useState, useEffect, useRef } from "react";
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
import { AuthProvider, useAuth } from "@/lib/auth-context";
import { LoginOverlay } from "@/components/login/LoginOverlay";
import { AppLayout } from "@/components/layout/AppLayout";
import { ChatView } from "@/components/chat/ChatView";
import { API_BASE } from "@/lib/constants";
import { SessionProvider } from "@/components/session/SessionProvider";
import { SessionList } from "@/components/session/SessionList";
import { shouldLogChatDebug } from "@/chat-debug";
import { extractLatestUserText } from "@/lib/chat-request";
import { sessionStore } from "@/lib/session-store";
import { chatStore } from "@/lib/chat-store";

type ChatRequestState = "idle" | "sending" | "streaming";

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

  const transportRef = useRef(transportOptions);
  useEffect(() => {
    transportRef.current = transportOptions;
  });
  const transport = useMemo(() => {
    const ref = transportRef;
    return new Proxy(ref, {
      get(_, prop) {
        const res = ref.current?.[prop as keyof typeof ref.current];
        return typeof res === "function" ? res.bind(ref.current) : res;
      },
    }) as unknown as AssistantChatTransport<UIMessage>;
  }, [transportRef]);

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
  const { isAuthenticated, isLoading } = useAuth();
  const [chatError, setChatError] = useState<string | null>(null);
  const [requestState, setRequestState] = useState<ChatRequestState>("idle");

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
          const response = await fetch(input, init);
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
    onData: (part: { type: string; data?: unknown }) => {
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
      setRequestState("idle");
      setChatError(error.message);
    },
    onFinish: () => {
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
    return <LoginOverlay />;
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <SessionProvider>
        <AppLayout sidebar={<SessionList />}>
          <ChatView
            chatError={chatError}
            requestState={requestState}
            onComposerSend={() => {
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
