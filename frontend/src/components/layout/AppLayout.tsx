import { useState, type ReactNode } from "react";
import { useAuth } from "@/lib/auth-context-store";
import { SettingsPanel } from "@/components/settings/SettingsPanel";

export function AppLayout({
  sidebar,
  children,
  navigationDisabled = false,
}: {
  sidebar: ReactNode;
  children: ReactNode;
  navigationDisabled?: boolean;
}) {
  const { email, logout } = useAuth();
  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground">
      <aside className="flex w-[260px] shrink-0 flex-col border-r border-border bg-surface">
        <div className={navigationDisabled ? "pointer-events-none opacity-50" : ""}>
          {sidebar}
        </div>
        <div className="shrink-0 border-t border-border p-3">
          {email && (
            <div className="flex items-center justify-between gap-2 px-1">
              <span className="truncate text-xs text-muted-foreground">{email}</span>
              <div className="flex items-center gap-1">
                <button
                  className="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-accent/10 hover:text-accent"
                  onClick={() => setSettingsOpen(true)}
                >
                  Settings
                </button>
                <button
                  className="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-danger/10 hover:text-danger"
                  onClick={logout}
                >
                  Sign out
                </button>
              </div>
            </div>
          )}
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden text-foreground">
        {children}
      </main>

      <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
