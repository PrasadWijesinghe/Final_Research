import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Container,
  GitBranch,
  Github,
  LoaderCircle,
  ServerCog,
  ShieldCheck,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";

type RiskOutput = {
  prediction_id: string;
  probability: number;
  threshold: number;
  alert: boolean;
  status: "ok" | "degraded" | "error";
  error_message: string | null;
  model_version: string;
  latency_ms: number;
};

type LatestPushPrediction = {
  repo: string;
  branch: string;
  head_sha: string;
  changed_files: string[];
  received_at: string;
  prediction: RiskOutput;
  source: "push" | "pre_push";
  initial_snapshot: boolean;
};

type MonitorStatus = {
  repository: string | null;
  branch: string | null;
  state: "disconnected" | "connecting" | "connected" | "error";
  checked_at: string | null;
  error: string | null;
  interval_seconds: number;
};

type FormState = {
  owner: string;
  repository: string;
  branch: string;
  additions: number;
  deletions: number;
  runner: "ubuntu-latest" | "self-hosted";
  workflowChanged: boolean;
  dockerUsed: boolean;
  deployChanged: boolean;
  permissions: boolean;
  matrix: boolean;
  cache: boolean;
  timeout: boolean;
  continueOnError: boolean;
};

const initialForm: FormState = {
  owner: "example-org",
  repository: "checkout-service",
  branch: "feature/safer-deploy",
  additions: 74,
  deletions: 18,
  runner: "ubuntu-latest",
  workflowChanged: true,
  dockerUsed: true,
  deployChanged: false,
  permissions: true,
  matrix: false,
  cache: true,
  timeout: true,
  continueOnError: false,
};

const options: Array<{ key: keyof FormState; label: string; hint: string }> = [
  { key: "workflowChanged", label: "Workflow changed", hint: ".github/workflows" },
  { key: "dockerUsed", label: "Docker used", hint: "image or Dockerfile" },
  { key: "deployChanged", label: "Deploy config changed", hint: "K8s, Helm, Vercel" },
  { key: "permissions", label: "Permissions defined", hint: "explicit job access" },
  { key: "matrix", label: "Matrix build", hint: "multiple variants" },
  { key: "cache", label: "Dependency cache", hint: "actions/cache" },
  { key: "timeout", label: "Timeout set", hint: "timeout-minutes" },
  { key: "continueOnError", label: "Continue on error", hint: "non-blocking step" },
];

function hexId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(20));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function workflowYaml(form: FormState): string {
  const lines = [
    "name: CI",
    "on: [push]",
    form.permissions ? "permissions:\n  contents: read" : "",
    "jobs:",
    "  verify:",
    `    runs-on: ${form.runner}`,
    form.timeout ? "    timeout-minutes: 15" : "",
    form.continueOnError ? "    continue-on-error: true" : "",
    form.matrix ? "    strategy:\n      matrix:\n        node: [20, 22]" : "",
    form.dockerUsed ? "    container: node:22" : "",
    "    steps:",
    "      - uses: actions/checkout@v4",
    form.cache ? "      - uses: actions/cache@v4" : "",
    "      - run: npm ci && npm test",
  ];
  return lines.filter(Boolean).join("\n");
}

function buildContext(form: FormState) {
  const now = new Date().toISOString();
  const files = [
    {
      path: "src/app.ts",
      status: "modified",
      additions: form.additions,
      deletions: form.deletions,
      patch: null,
      tags: ["source"],
    },
  ];
  if (form.workflowChanged) {
    files.push({
      path: ".github/workflows/ci.yml",
      status: "modified",
      additions: 12,
      deletions: 3,
      patch: null,
      tags: ["workflow"],
    });
  }
  if (form.dockerUsed) {
    files.push({
      path: "Dockerfile",
      status: "modified",
      additions: 4,
      deletions: 1,
      patch: null,
      tags: ["dockerfile"],
    });
  }
  if (form.deployChanged) {
    files.push({
      path: "k8s/deployment.yml",
      status: "modified",
      additions: 8,
      deletions: 2,
      patch: null,
      tags: ["deploy_config"],
    });
  }

  const workflow = workflowYaml(form);
  return {
    schema_version: "1.1.0",
    repo: { owner: form.owner, name: form.repository },
    head_sha: hexId(),
    base_sha: null,
    event: "pre_push",
    branch: form.branch,
    pr_number: null,
    author_hash: "0".repeat(63) + "1",
    committed_at: now,
    history_cutoff: now,
    changed_files: files,
    snapshots: form.workflowChanged
      ? [
          {
            path: ".github/workflows/ci.yml",
            tag: "workflow",
            base_content: null,
            head_content: workflow,
            base_ref: null,
            head_ref: null,
            changed: true,
          },
        ]
      : [],
    workflow_jobs: [
      {
        workflow_path: ".github/workflows/ci.yml",
        job_key: "verify",
        job_name: "Verify push",
        runs_on: [form.runner],
        node_versions: form.matrix ? ["20", "22"] : ["22"],
        permissions: form.permissions ? { contents: "read" } : {},
        uses_container: form.dockerUsed,
        commands: ["npm ci", "npm test"],
      },
    ],
    is_synthetic: false,
    truncated: false,
  };
}

function Toggle({
  checked,
  label,
  hint,
  onChange,
}: {
  checked: boolean;
  label: string;
  hint: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="toggle-row">
      <span>
        <strong>{label}</strong>
        <small>{hint}</small>
      </span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span className="toggle" aria-hidden="true" />
    </label>
  );
}

function App() {
  const [form, setForm] = useState(initialForm);
  const [result, setResult] = useState<RiskOutput | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [listening, setListening] = useState(false);
  const [livePush, setLivePush] = useState<LatestPushPrediction | null>(null);
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [monitorBranch, setMonitorBranch] = useState("main");
  const [monitor, setMonitor] = useState<MonitorStatus | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [history, setHistory] = useState<LatestPushPrediction[]>([]);
  const latestPredictionId = useRef<string | null>(null);

  const changedFiles = useMemo(
    () => 1 + Number(form.workflowChanged) + Number(form.dockerUsed) + Number(form.deployChanged),
    [form],
  );

  function update<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  useEffect(() => {
    let active = true;
    let inFlight = false;
    async function checkForPush() {
      if (inFlight) return;
      inFlight = true;
      try {
        const responses = await Promise.all([
          fetch("/api/monitor/status", { cache: "no-store", signal: AbortSignal.timeout(5000) }),
          fetch("/api/monitor/history", { cache: "no-store", signal: AbortSignal.timeout(5000) }),
        ]);
        if (responses.some((response) => !response.ok)) throw new Error("Monitor unavailable");
        const status = (await responses[0].json()) as MonitorStatus;
        const events = (await responses[1].json()) as LatestPushPrediction[];
        if (!active) return;
        setListening(true);
        setMonitor(status);
        const connectedRepo = status.repository
          ?.replace(/^https:\/\/github\.com\//, "")
          .replace(/^git@github\.com:/, "").replace(/\.git$/, "");
        const relevant = connectedRepo
          ? events.filter((item) => item.repo === connectedRepo && item.branch === status.branch)
          : events;
        setHistory(relevant);
        const latest = relevant[0];
        if (latest && latest.prediction.prediction_id !== latestPredictionId.current) {
          latestPredictionId.current = latest.prediction.prediction_id;
          setLivePush(latest);
          setResult(latest.prediction);
          setError(null);
        }
      } catch {
        if (active) setListening(false);
      } finally {
        inFlight = false;
      }
    }
    void checkForPush();
    const timer = window.setInterval(() => void checkForPush(), 1500);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  async function changeConnection(disconnect = false) {
    setConnecting(true);
    setConnectionError(null);
    try {
      const response = await fetch(`/api/monitor/${disconnect ? "disconnect" : "connect"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        ...(disconnect ? {} : { body: JSON.stringify({ repository: repositoryUrl, branch: monitorBranch }) }),
      });
      if (!response.ok) {
        const body = await response.json();
        const detail = typeof body.detail === "string" ? body.detail
          : "Check the GitHub repository URL and branch name.";
        throw new Error(detail);
      }
      setMonitor(await response.json());
      setLivePush(null);
      setResult(null);
      setHistory([]);
      latestPredictionId.current = null;
    } catch (reason) {
      setConnectionError(reason instanceof Error ? reason.message : "Connection failed.");
    } finally {
      setConnecting(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const response = await fetch("/api/predict", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildContext(form)),
      });
      if (!response.ok) {
        const body = await response.text();
        throw new Error(`Prediction service returned ${response.status}: ${body}`);
      }
      const outputs = (await response.json()) as RiskOutput[];
      if (!outputs[0]) throw new Error("Prediction service returned no Infra result.");
      latestPredictionId.current = outputs[0].prediction_id;
      setLivePush(null);
      setResult(outputs[0]);
    } catch (reason) {
      setResult(null);
      setError(reason instanceof Error ? reason.message : "Prediction failed.");
    } finally {
      setLoading(false);
    }
  }

  const percentage = result
    ? `${(result.probability * 100).toFixed(result.probability < 0.01 ? 2 : 1)}%`
    : null;

  return (
    <main>
      <header className="topbar">
        <div className="brand-mark"><Activity size={20} /></div>
        <div>
          <p className="eyebrow">PipelineIQ</p>
          <h1>Infrastructure risk check</h1>
        </div>
        <div className={`research-state ${listening ? "connected" : ""}`}>
          <span /> {!listening ? "API offline" : monitor?.state === "connected" ? "Monitoring remote branch" : "API online"}
        </div>
      </header>

      <section className="intro">
        <div>
          <p className="kicker">Live repository monitoring</p>
          <h2>Estimate whether this push could fail for infrastructure reasons.</h2>
          <p>
            Connect a GitHub repository. When its branch changes, the trained Infra model estimates
            failure risk and the result appears here. You can also try a manual scenario below.
          </p>
        </div>
        <div className="flow" aria-label="Prediction flow">
          <span><Github size={18} /> Push</span>
          <ArrowRight size={18} />
          <span><ServerCog size={18} /> Infra model</span>
          <ArrowRight size={18} />
          <span><ShieldCheck size={18} /> Risk estimate</span>
        </div>
      </section>

      <section className="panel connection-panel" aria-label="Repository connection">
        <h3>Connect one repository</h3>
        <form onSubmit={(event) => { event.preventDefault(); void changeConnection(); }}>
          <div className="field-grid two">
            <label>GitHub repository URL<input placeholder="https://github.com/owner/repository" value={repositoryUrl} onChange={(event) => setRepositoryUrl(event.target.value)} required /></label>
            <label>Branch to monitor<input value={monitorBranch} onChange={(event) => setMonitorBranch(event.target.value)} required /></label>
          </div>
          <button className="primary" disabled={connecting} type="submit">{connecting ? "Connecting…" : "Connect repository"}</button>
          {monitor?.state !== "disconnected" && monitor ? <button type="button" disabled={connecting} onClick={() => void changeConnection(true)}>Disconnect</button> : null}
        </form>
        <p role="status">{!listening ? "Start the Infra API to connect." : monitor?.repository
          ? `${monitor.state}: ${monitor.repository} · ${monitor.branch}` : "No remote repository connected."}</p>
        {monitor?.checked_at ? <p>Last check: {new Date(monitor.checked_at).toLocaleTimeString()} · every {monitor.interval_seconds} seconds</p> : null}
        {connectionError || monitor?.error ? <p role="alert">{connectionError ?? monitor?.error}</p> : null}
        <p>Public repositories work directly. Private repositories use this computer’s existing Git credentials. The first result analyzes the current branch snapshot; later results analyze newly observed changes.</p>
      </section>

      <div className="workspace">
        <form className="panel input-panel" onSubmit={submit}>
          <div className="panel-heading">
            <div>
              <p className="step">01 · Push details</p>
              <h3>Manual scenario (optional)</h3>
            </div>
            <GitBranch size={23} />
          </div>

          <div className="field-grid two">
            <label>Owner<input value={form.owner} onChange={(e) => update("owner", e.target.value)} required /></label>
            <label>Repository<input value={form.repository} onChange={(e) => update("repository", e.target.value)} required /></label>
          </div>
          <label>Branch<input value={form.branch} onChange={(e) => update("branch", e.target.value)} required /></label>
          <div className="field-grid two">
            <label>Lines added<input type="number" min="0" value={form.additions} onChange={(e) => update("additions", Number(e.target.value))} /></label>
            <label>Lines deleted<input type="number" min="0" value={form.deletions} onChange={(e) => update("deletions", Number(e.target.value))} /></label>
          </div>
          <label>
            Runner
            <select value={form.runner} onChange={(e) => update("runner", e.target.value as FormState["runner"])}>
              <option value="ubuntu-latest">GitHub-hosted · ubuntu-latest</option>
              <option value="self-hosted">Self-hosted runner</option>
            </select>
          </label>

          <div className="divider" />
          <p className="step">02 · Infrastructure signals</p>
          <div className="toggle-grid">
            {options.map((option) => (
              <Toggle
                key={option.key}
                checked={Boolean(form[option.key])}
                label={option.label}
                hint={option.hint}
                onChange={(checked) => update(option.key, checked)}
              />
            ))}
          </div>

          <button className="primary" disabled={loading} type="submit">
            {loading ? <LoaderCircle className="spin" size={19} /> : <Activity size={19} />}
            {loading ? "Calculating risk…" : "Predict this push"}
          </button>
        </form>

        <aside className="panel result-panel" aria-live="polite">
          <div className="panel-heading">
            <div>
              <p className="step">03 · Result</p>
              <h3>Infra failure probability</h3>
            </div>
            <Container size={23} />
          </div>

          {error ? (
            <div className="error-state">
              <AlertTriangle size={28} />
              <h4>Could not reach the model</h4>
              <p>{error}</p>
              <small>Start the Infra API on port 8104, then try again.</small>
            </div>
          ) : result ? (
            <div className="result-content">
              {livePush ? (
                <div className="live-push">
                  <GitBranch size={17} />
                  <div>
                    <strong>{livePush.initial_snapshot ? "Initial branch snapshot" : livePush.source === "push" ? "Remote branch update detected" : "Local pre-push detected"}</strong>
                    <span>{livePush.repo} · {livePush.branch} · {livePush.head_sha.slice(0, 8)}</span>
                  </div>
                </div>
              ) : null}
              <div className={`risk-orbit ${result.alert ? "high" : "low"}`}>
                <div>
                  <strong>{percentage}</strong>
                  <span>estimated risk</span>
                </div>
              </div>
              <div className={`verdict ${result.alert ? "warning" : "clear"}`}>
                {result.alert ? <AlertTriangle size={20} /> : <CheckCircle2 size={20} />}
                <div>
                  <strong>{result.alert ? "Above alert threshold" : "Below alert threshold"}</strong>
                  <span>Threshold: {(result.threshold * 100).toFixed(1)}%</span>
                </div>
              </div>
              <dl className="result-facts">
                <div><dt>Model</dt><dd>{result.model_version}</dd></div>
                <div>
                  <dt>Inputs</dt>
                  <dd>{livePush ? livePush.changed_files.length : changedFiles} changed files</dd>
                </div>
                <div><dt>Latency</dt><dd>{result.latency_ms} ms</dd></div>
              </dl>
              <div className="research-note">
                <AlertTriangle size={17} />
                <p>{result.error_message ?? "Research estimate — not a deployment decision."}</p>
              </div>
            </div>
          ) : (
            <div className="empty-state">
              <Activity size={42} strokeWidth={1.4} />
              <h4>Your prediction will appear here</h4>
              <p>Run git push in a connected project, or use the form to test the model.</p>
              <div className="mini-facts">
                <span><b>{changedFiles}</b> files represented</span>
                <span><b>{form.additions + form.deletions}</b> changed lines</span>
              </div>
            </div>
          )}
        </aside>
      </div>

      {history.length > 0 ? <section className="panel connection-panel" aria-label="Recent predictions">
        <h3>Recent predictions</h3>
        <p>Latest 50 results from this API session. Click a result to inspect it.</p>
        <ul>{history.map((item) => <li key={item.prediction.prediction_id}>
          <button type="button" onClick={() => { setLivePush(item); setResult(item.prediction); setError(null); }}>
            {item.repo} · {item.branch} · {item.head_sha.slice(0, 8)} · {(item.prediction.probability * 100).toFixed(2)}% · {new Date(item.received_at).toLocaleTimeString()}
          </button>
        </li>)}</ul>
      </section> : null}

      <footer>
        <span>Model output is a research estimate, not a guarantee that CI will pass or fail.</span>
        <span>v4 · no-history profile</span>
      </footer>
    </main>
  );
}

export default App;
