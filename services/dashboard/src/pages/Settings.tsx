import { Copy, KeyRound, Trash2 } from "lucide-react";
import { useState } from "react";
import { AsyncView, Empty, Page } from "../components/ui";
import { api } from "../lib/api";
import { when } from "../lib/risk";
import { useAsync } from "../lib/useAsync";
import type { ApiKeyIssued } from "../types/api";

export default function Settings() {
  const [state, reload] = useAsync(() => api.listKeys(), []);
  const [issued, setIssued] = useState<ApiKeyIssued | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      setIssued(await api.createKey());
      setCopied(false);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const revoke = async (id: string) => {
    setError(null);
    try {
      await api.revokeKey(id);
      if (issued?.id === id) setIssued(null);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const copy = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.key);
      setCopied(true);
    } catch {
      setError("Could not copy automatically. Select the key and copy it by hand.");
    }
  };
  const dashUrl = window.location.origin;

  return (
    <Page title="Settings" kicker="API keys for the pre-push hook" actions={
      <button className="pq-btn pq-primary" onClick={() => void create()} disabled={busy}><KeyRound size={14} /> Create API key</button>
    }>
      {error && <p className="pq-err" role="alert">{error}</p>}
      {issued && (
        <div className="pq-card pq-key" role="status">
          <strong>Your new key</strong>
          <p className="pq-warn">Copy it now. It is shown only once and cannot be recovered; if you lose it, revoke it and create another.</p>
          <div className="pq-keyrow">
            <code data-testid="new-key">{issued.key}</code>
            <button className="pq-btn" onClick={() => void copy()}><Copy size={14} /> {copied ? "Copied" : "Copy"}</button>
            <button className="pq-btn pq-ghost" onClick={() => setIssued(null)}>Done</button>
          </div>
        </div>
      )}
      <AsyncView state={state} reload={reload} what="Loading keys" isEmpty={(k) => k.length === 0} empty={<Empty title="No API keys yet" hint="Create one to let the pre-push hook talk to the API." />}>
        {(keys) => (
          <div className="pq-scroll">
            <table className="pq-table">
              <thead><tr><th>Key id</th><th>Created</th><th>Status</th><th /></tr></thead>
              <tbody>
                {keys.map((k) => (
                  <tr key={k.id}>
                    <td className="pq-mono">{k.id}</td>
                    <td className="pq-small">{when(k.created_at)}</td>
                    <td>{k.revoked_at ? <span className="pq-dim">revoked {when(k.revoked_at)}</span> : "active"}</td>
                    <td>{!k.revoked_at && <button className="pq-btn pq-ghost" onClick={() => void revoke(k.id)}><Trash2 size={14} /> Revoke</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </AsyncView>
      <section className="pq-card">
        <h3>Use a key in the pre-push hook</h3>
        <ol>
          <li>Create a key above and copy it.</li>
          <li>Put it in the file <code>~/.pipelineiq</code> in your home folder:</li>
        </ol>
        <pre className="pq-pre">{`api_key=<paste your key here>\ndashboard_url=${dashUrl}`}</pre>
        <p className="pq-dim pq-small">
          <code>dashboard_url</code> is where the hook sends you to see the risks of a commit (<code>{dashUrl}/#/commits/&lt;sha&gt;</code>).
          Check the hook&apos;s own README for the exact file format.
        </p>
      </section>
    </Page>
  );
}
