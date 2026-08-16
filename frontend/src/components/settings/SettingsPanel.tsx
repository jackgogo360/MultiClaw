import { useState } from "react";
import { DeletionSettings } from "./DeletionSettings";
import { SecretSettings } from "./SecretSettings";

type SettingsTab = "secrets" | "deletion";

export function SettingsPanel({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<SettingsTab>("secrets");

  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/55 px-4 py-8 backdrop-blur-sm">
      <div className="flex max-h-full w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-2xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold text-foreground">Settings</h2>
            <p className="text-sm text-muted-foreground">
              Manage secrets and account lifecycle without leaving the current session.
            </p>
          </div>
          <button
            className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:border-accent hover:text-accent"
            onClick={onClose}
          >
            Close
          </button>
        </div>

        <div className="flex flex-wrap gap-2 border-b border-border px-5 py-3">
          <button
            className={`rounded-full px-3 py-1.5 text-sm ${
              tab === "secrets"
                ? "bg-accent text-background"
                : "border border-border text-muted-foreground hover:border-accent hover:text-accent"
            }`}
            onClick={() => setTab("secrets")}
          >
            Secrets
          </button>
          <button
            className={`rounded-full px-3 py-1.5 text-sm ${
              tab === "deletion"
                ? "bg-accent text-background"
                : "border border-border text-muted-foreground hover:border-accent hover:text-accent"
            }`}
            onClick={() => setTab("deletion")}
          >
            Deletion
          </button>
        </div>

        <div className="overflow-y-auto px-5 py-4">
          {tab === "secrets" ? <SecretSettings /> : <DeletionSettings />}
        </div>
      </div>
    </div>
  );
}
