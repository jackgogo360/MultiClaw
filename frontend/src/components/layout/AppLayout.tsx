import { type ReactNode } from "react";
import { useAuth } from "@/lib/auth-context";

export function AppLayout({
  sidebar,
  children,
}: {
  sidebar: ReactNode;
  children: ReactNode;
}) {
  const { email, logout } = useAuth();

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground">
      <aside className="flex w-[260px] shrink-0 flex-col border-r border-border bg-surface">
        {sidebar}
        <div className="shrink-0 border-t border-border p-3">
          {email && (
            <div className="flex items-center justify-between px-1">
              <span className="truncate text-xs text-muted-foreground">{email}</span>
              <button
                className="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-danger/10 hover:text-danger"
                onClick={logout}
              >
                Sign out
              </button>
            </div>
          )}
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden text-foreground">
        {children}
      </main>
    </div>
  );
}
