import { useEffect, useState } from "react";
import { ApiError, secretApi, type SecretMetadata } from "@/lib/api";

const DEFAULT_SECRET = {
  providerKind: "llm",
  providerName: "openai",
  secretName: "api_key",
};

function formatError(error: unknown) {
  if (error instanceof ApiError) {
    return error.message;
  }
  return error instanceof Error ? error.message : "Request failed.";
}

export function SecretSettings() {
  const [secrets, setSecrets] = useState<SecretMetadata[]>([]);
  const [providerKind, setProviderKind] = useState(DEFAULT_SECRET.providerKind);
  const [providerName, setProviderName] = useState(DEFAULT_SECRET.providerName);
  const [secretName, setSecretName] = useState(DEFAULT_SECRET.secretName);
  const [secretValue, setSecretValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadSecrets = async () => {
    try {
      const next = await secretApi.list();
      setSecrets(next);
      setError(null);
    } catch (nextError) {
      setError(formatError(nextError));
    }
  };

  useEffect(() => {
    let cancelled = false;

    void secretApi
      .list()
      .then((next) => {
        if (cancelled) {
          return;
        }
        setSecrets(next);
        setError(null);
      })
      .catch((nextError) => {
        if (cancelled) {
          return;
        }
        setError(formatError(nextError));
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const handleSave = async () => {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      await secretApi.put(providerKind, providerName, secretName, secretValue);
      await loadSecrets();
      setMessage("Secret saved.");
    } catch (nextError) {
      setError(formatError(nextError));
    } finally {
      setSecretValue("");
      setBusy(false);
    }
  };

  const handleTest = async (entry: SecretMetadata) => {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const result = await secretApi.test(
        entry.providerKind,
        entry.providerName,
        entry.secretName,
      );
      setMessage(result.ok ? "Secret validation succeeded." : "Secret validation failed.");
    } catch (nextError) {
      setError(formatError(nextError));
    } finally {
      setSecretValue("");
      setBusy(false);
    }
  };

  const handleDelete = async (entry: SecretMetadata) => {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      await secretApi.del(entry.providerKind, entry.providerName, entry.secretName);
      await loadSecrets();
      setMessage("Secret deleted.");
    } catch (nextError) {
      setError(formatError(nextError));
    } finally {
      setSecretValue("");
      setBusy(false);
    }
  };

  return (
    <section className="space-y-4">
      <div className="space-y-1">
        <h3 className="text-sm font-semibold text-foreground">Provider secrets</h3>
        <p className="text-sm text-muted-foreground">
          Values are only sent in the current request. The UI stores metadata and masked values only.
        </p>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        <label className="space-y-1 text-sm">
          <span className="text-muted-foreground">Provider kind</span>
          <input
            className="w-full rounded-lg border border-border bg-input px-3 py-2 text-sm"
            value={providerKind}
            onChange={(event) => setProviderKind(event.target.value)}
          />
        </label>
        <label className="space-y-1 text-sm">
          <span className="text-muted-foreground">Provider name</span>
          <input
            className="w-full rounded-lg border border-border bg-input px-3 py-2 text-sm"
            value={providerName}
            onChange={(event) => setProviderName(event.target.value)}
          />
        </label>
        <label className="space-y-1 text-sm">
          <span className="text-muted-foreground">Secret name</span>
          <input
            className="w-full rounded-lg border border-border bg-input px-3 py-2 text-sm"
            value={secretName}
            onChange={(event) => setSecretName(event.target.value)}
          />
        </label>
      </div>

      <label className="space-y-1 text-sm">
        <span className="text-muted-foreground">Secret value</span>
        <input
          type="password"
          className="w-full rounded-lg border border-border bg-input px-3 py-2 text-sm"
          value={secretValue}
          onChange={(event) => setSecretValue(event.target.value)}
          placeholder="Only kept in memory until the request finishes"
        />
      </label>

      <div className="flex items-center gap-3">
        <button
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-background hover:brightness-110 disabled:opacity-50"
          onClick={handleSave}
          disabled={busy || !providerKind || !providerName || !secretName || !secretValue}
        >
          Save secret
        </button>
        <button
          className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:border-accent hover:text-accent"
          onClick={() => void loadSecrets()}
          disabled={busy}
        >
          Refresh
        </button>
      </div>

      {error ? (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </div>
      ) : null}
      {message ? (
        <div className="rounded-lg border border-success/30 bg-success/10 px-3 py-2 text-sm text-success">
          {message}
        </div>
      ) : null}

      <div className="space-y-3">
        {secrets.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border px-4 py-4 text-sm text-muted-foreground">
            No stored secrets yet.
          </div>
        ) : (
          secrets.map((entry) => (
            <article
              key={`${entry.providerKind}:${entry.providerName}:${entry.secretName}`}
              className="rounded-xl border border-border bg-background/70 p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="font-medium text-foreground">
                    {entry.providerKind}:{entry.providerName}
                  </div>
                  <div className="text-sm text-muted-foreground">{entry.secretName}</div>
                </div>
                <div className="text-right text-sm text-muted-foreground">
                  <div>{entry.maskedValue}</div>
                  <div>{new Date(entry.updatedAt).toLocaleString()}</div>
                </div>
              </div>
              <div className="mt-3 flex gap-2">
                <button
                  className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:border-accent hover:text-accent"
                  onClick={() => void handleTest(entry)}
                  disabled={busy}
                >
                  Test
                </button>
                <button
                  className="rounded-lg border border-border px-3 py-1.5 text-sm text-muted-foreground hover:border-danger hover:text-danger"
                  onClick={() => void handleDelete(entry)}
                  disabled={busy}
                >
                  Delete
                </button>
              </div>
            </article>
          ))
        )}
      </div>
    </section>
  );
}
