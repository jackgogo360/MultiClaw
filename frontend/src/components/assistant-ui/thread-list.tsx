import {
  ThreadListPrimitive,
  ThreadListItemPrimitive,
} from "@assistant-ui/react";

export function ThreadList() {
  return (
    <ThreadListPrimitive.Root className="flex flex-col h-full bg-surface border-r border-border">
      <div className="flex items-center justify-between p-4 border-b border-border">
        <h2 className="text-lg font-semibold text-foreground">Chats</h2>
        <ThreadListPrimitive.New>
          <button className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-background hover:brightness-110 transition-all">
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
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            New Chat
          </button>
        </ThreadListPrimitive.New>
      </div>

      <ThreadListPrimitive.Items>
        {() => (
          <div className="flex flex-col gap-1 p-2">
            <ThreadListItem />
          </div>
        )}
      </ThreadListPrimitive.Items>
    </ThreadListPrimitive.Root>
  );
}

function ThreadListItem() {
  return (
    <ThreadListItemPrimitive.Root className="group flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:bg-elevated hover:text-foreground transition-colors cursor-pointer data-[active=true]:bg-elevated data-[active=true]:text-foreground">
      <ThreadListItemPrimitive.Trigger className="flex-1 text-left truncate">
        <ThreadListItemPrimitive.Title />
      </ThreadListItemPrimitive.Trigger>
      <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
        <ThreadListItemPrimitive.Delete>
          <button className="rounded p-1 hover:bg-danger/10 hover:text-danger transition-colors">
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polyline points="3 6 5 6 21 6" />
              <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
            </svg>
          </button>
        </ThreadListItemPrimitive.Delete>
      </div>
    </ThreadListItemPrimitive.Root>
  );
}
