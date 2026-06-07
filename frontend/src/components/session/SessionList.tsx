import { useSessions } from "./SessionProvider";

export function SessionList() {
  const { sessions, currentId, switchSession, deleteSession, createSession } = useSessions();

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-border px-3 py-2">
        <button
          className="w-full rounded-lg border border-dashed border-border py-2 text-sm text-muted-foreground hover:border-accent hover:text-accent"
          onClick={createSession}
        >
          + New Chat
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
        {sessions.map((s) => (
          <div
            key={s.id}
            className={`group flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm transition-colors ${
              s.id === currentId
                ? "bg-accent/10 text-accent"
                : "text-muted-foreground hover:bg-elevated hover:text-foreground"
            }`}
            onClick={() => switchSession(s.id)}
          >
            <span className="flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-md bg-elevated text-[10px]">
              {s.id === currentId ? "●" : "○"}
            </span>
            <span className="flex-1 truncate">{s.title}</span>
            <button
              className="hidden rounded p-0.5 text-muted-foreground hover:bg-danger/10 hover:text-danger group-hover:block"
              onClick={(e) => {
                e.stopPropagation();
                if (confirm("Delete this conversation?")) deleteSession(s.id);
              }}
            >
              &times;
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
