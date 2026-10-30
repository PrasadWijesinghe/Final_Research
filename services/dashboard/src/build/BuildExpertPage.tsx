import {
  AlertTriangle,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  CheckCircle2,
  FileCode2,
  GitCommitHorizontal,
  LoaderCircle,
  RefreshCw,
  Stethoscope,
  Upload,
  Wrench,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  actualFromName,
  diagnose,
  type Diagnosis,
  type Example,
  type ExampleKind,
  getExample,
  getExamples,
  getModel,
  type Input,
  levelOf,
  type ModelSummary,
  pct,
  predict,
  type RiskOutput,
} from "./api";
import "./build-expert.css";

type Mode = "predict" | "diagnose";
type ApiState = "checking" | "connected" | "offline";

const KIND: Record<Mode, ExampleKind> = { predict: "commits", diagnose: "failures" };
const TOOL_NAMES: Record<string, string> = { tsc: "TypeScript (tsc)", eslint: "ESLint", bundler: "Bundler", other: "Build (format or other)" };
const SUB_RISK_NAMES: Record<string, string> = { tsc: "TypeScript", lint: "Lint and format", bundler: "Bundler" };

function ModelStrip({ model }: { model: ModelSummary | null }) {
  if (!model) return null;
  if (!model.loaded) {
    return (
      <div className="bx-note warn">
        <AlertTriangle size={15} /> No trained model is loaded, so the expert answers with its placeholder. Start it with
        <code>uv run python demo-kit/run_api.py</code>.
      </div>
    );
  }
  const t = model.test;
  return (
    <div className="bx-stats" aria-label="Model facts">
      <div>
        <span>Model</span>
        <strong>{model.model_version}</strong>
        <small>LightGBM, 5 seeds averaged, calibrated</small>
      </div>
      <div>
        <span>Alert threshold</span>
        <strong>{pct(model.threshold)}</strong>
        <small>chance of a build failure</small>
      </div>
      <div>
        <span>Test F1</span>
        <strong>{t?.f1 != null ? t.f1.toFixed(3) : "n/a"}</strong>
        <small>best baseline {model.best_baseline?.f1 != null ? model.best_baseline.f1.toFixed(3) : "n/a"}</small>
      </div>
      <div>
        <span>Test PR-AUC</span>
        <strong>{t?.pr_auc != null ? t.pr_auc.toFixed(3) : "n/a"}</strong>
        <small>
          {t?.commits != null ? `${t.commits.toLocaleString()} newest commits, ${t.failures ?? "?"} failed` : "newest commits"}
        </small>
      </div>
    </div>
  );
}

function InputPanel({
  mode,
  examples,
  selected,
  onSelect,
  upload,
  onUpload,
  onRun,
  busy,
  apiState,
}: {
  mode: Mode;
  examples: Example[];
  selected: string | null;
  onSelect: (name: string) => void;
  upload: Input | null;
  onUpload: (input: Input | null, error?: string) => void;
  onRun: () => void;
  busy: boolean;
  apiState: ApiState;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const mine = examples.filter((e) => e.kind === KIND[mode]);
  const what = mode === "predict" ? "commit" : "failed job";

  const readFile = async (file?: File) => {
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) return onUpload(null, "That file is larger than 20 MB.");
    try {
      onUpload({ label: file.name, body: JSON.parse(await file.text()), actual: actualFromName(file.name) });
    } catch {
      onUpload(null, `${file.name} is not valid JSON.`);
    }
  };

  return (
    <section className="bx-panel" aria-labelledby="bx-input-title">
      <div className="bx-panel-heading">
        <div>
          <p className="bx-step">{mode === "predict" ? "01 / Before CI" : "02 / After a failure"}</p>
          <h2 id="bx-input-title">{mode === "predict" ? "Pick a real commit" : "Pick a real failed job"}</h2>
        </div>
        <div className="bx-heading-icon">{mode === "predict" ? <GitCommitHorizontal size={18} /> : <Stethoscope size={18} />}</div>
      </div>

      {mine.length > 0 ? (
        <div className="bx-examples" role="radiogroup" aria-label={`Example ${what}s`}>
          {mine.map((e) => {
            const actual = actualFromName(e.name);
            return (
              <button
                key={e.name}
                type="button"
                role="radio"
                aria-checked={selected === e.name}
                className={`bx-example ${selected === e.name ? "selected" : ""}`}
                onClick={() => onSelect(e.name)}
              >
                <span className="bx-radio" aria-hidden="true" />
                <span className="bx-example-text">{e.label}</span>
                {actual ? <span className={`bx-chip ${actual}`}>{actual === "failed" ? "really failed" : "really passed"}</span> : null}
              </button>
            );
          })}
        </div>
      ) : (
        <p className="bx-muted">
          {apiState === "offline"
            ? "The Build expert is not reachable."
            : `No example ${what}s. Start the expert with the demo kit (run_api.py) to list them, or upload a JSON file below.`}
        </p>
      )}

      <div className="bx-upload">
        <input
          ref={fileInput}
          className="bx-file-input"
          type="file"
          accept=".json,application/json"
          aria-label={`Upload a ${mode === "predict" ? "CommitContext" : "RoutedFailure"} JSON file`}
          onChange={(event) => void readFile(event.target.files?.[0])}
        />
        <button type="button" className="bx-secondary" onClick={() => fileInput.current?.click()}>
          <Upload size={14} /> {upload ? upload.label : `Or upload a ${mode === "predict" ? "CommitContext" : "RoutedFailure"} JSON`}
        </button>
        {upload ? (
          <button type="button" className="bx-link" onClick={() => onSelect(upload.label)} aria-pressed={selected === upload.label}>
            {selected === upload.label ? "using the uploaded file" : "use the uploaded file"}
          </button>
        ) : null}
      </div>

      <button type="button" className="bx-primary" onClick={onRun} disabled={busy || !selected || apiState === "offline"}>
        {busy ? <LoaderCircle className="bx-spin" size={16} /> : mode === "predict" ? <GitCommitHorizontal size={16} /> : <Wrench size={16} />}
        {busy ? "Asking the Build expert…" : mode === "predict" ? "Predict build risk" : "Diagnose this failure"}
      </button>
    </section>
  );
}

function RiskResult({ input, out }: { input: Input; out: RiskOutput }) {
  const level = levelOf(out);
  const p = out.probability ?? 0;
  const maxShap = Math.max(0.0001, ...out.top_factors.map((f) => Math.abs(f.shap_value)));
  return (
    <section className="bx-panel bx-result" aria-live="polite">
      <div className="bx-panel-heading">
        <div>
          <p className="bx-step">Prediction · {input.label}</p>
          <h2>Chance this commit fails the build</h2>
        </div>
        <span className={`bx-level ${level}`}>{level === "high" ? "ALERT" : `${level} risk`}</span>
      </div>

      <div className="bx-big">
        <strong>{pct(out.probability)}</strong>
        <span>
          {out.alert ? "At or above the alert threshold: the developer would be warned." : "Below the alert threshold: no warning."}
        </span>
      </div>
      <div className="bx-gauge" role="img" aria-label={`Probability ${pct(out.probability)}, alert at ${pct(out.threshold)}`}>
        <div className={`bx-gauge-fill ${level}`} style={{ width: `${Math.min(100, p * 100)}%` }} />
        <div className="bx-gauge-mark" style={{ left: `${Math.min(100, out.threshold * 100)}%` }}>
          <span>alert at {pct(out.threshold)}</span>
        </div>
      </div>

      {input.actual ? (
        <p className={`bx-actual ${input.actual}`}>
          {input.actual === "failed" ? <AlertTriangle size={14} /> : <CheckCircle2 size={14} />}
          What really happened: the build {input.actual === "failed" ? "failed" : "passed"}.
        </p>
      ) : null}
      {out.status === "degraded" ? (
        <p className="bx-note warn">
          <AlertTriangle size={14} /> This repo has no history in the model's data, so its history features were missing.
        </p>
      ) : null}

      <h3 className="bx-sub">Which tool is most at risk</h3>
      <div className="bx-subrisks">
        {Object.entries(out.sub_risks).map(([k, v]) => (
          <div key={k} className="bx-subrisk">
            <span>{SUB_RISK_NAMES[k] ?? k}</span>
            <div className="bx-bar"><div style={{ width: `${Math.min(100, v * 100)}%` }} /></div>
            <strong>{pct(v, 0)}</strong>
          </div>
        ))}
      </div>

      <h3 className="bx-sub">Why (top risk factors, from SHAP)</h3>
      {out.top_factors.length === 0 ? (
        <p className="bx-muted">No single factor pushed the risk up much for this commit.</p>
      ) : (
        <ul className="bx-factors">
          {out.top_factors.map((f) => (
            <li key={f.feature}>
              <span className={`bx-dir ${f.direction}`} title={f.direction === "raises" ? "raises the risk" : "lowers the risk"}>
                {f.direction === "raises" ? <ArrowUp size={13} /> : <ArrowDown size={13} />}
              </span>
              <span className="bx-factor-text">
                {f.reason}
                <small>{f.feature}</small>
              </span>
              <span className="bx-impact" title={`SHAP ${f.shap_value.toFixed(3)}`}>
                <span style={{ width: `${(Math.abs(f.shap_value) / maxShap) * 100}%` }} />
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="bx-meta">
        Model {out.model_version} · answered in {out.latency_ms} ms · risk factors, not proven causes
      </p>
    </section>
  );
}

function DiagnosisResult({ input, d }: { input: Input; d: Diagnosis }) {
  const tool = d.details?.tool;
  const locations = d.details?.locations ?? [];
  return (
    <section className="bx-panel bx-result" aria-live="polite">
      <div className="bx-panel-heading">
        <div>
          <p className="bx-step">Diagnosis · {input.label}</p>
          <h2>
            {d.status === "misrouted"
              ? "Not a build failure"
              : d.status === "error"
                ? "The diagnosis failed"
                : `${TOOL_NAMES[tool ?? "other"] ?? "Build"} failure`}
          </h2>
        </div>
        {d.confidence != null ? <span className="bx-level medium">confidence {pct(d.confidence, 0)}</span> : null}
      </div>

      {d.status === "misrouted" ? (
        <p className="bx-note">
          <ArrowRight size={14} /> Handed back to the router: this looks like a <strong>{d.suggested_category}</strong> failure,
          so that member's expert should take it.
        </p>
      ) : null}
      {d.status === "error" ? <p className="bx-note warn">{d.error_message}</p> : null}
      {d.root_cause ? <p className="bx-cause">{d.root_cause}</p> : null}

      {locations.length > 0 ? (
        <>
          <h3 className="bx-sub">Where</h3>
          <table className="bx-table">
            <thead>
              <tr><th>File</th><th>Line</th><th>Error</th></tr>
            </thead>
            <tbody>
              {locations.slice(0, 8).map((l, i) => (
                <tr key={`${l.path}:${l.line}:${i}`}>
                  <td><FileCode2 size={12} /> {l.path}</td>
                  <td>{l.line ?? "?"}</td>
                  <td>{l.code ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {locations.length > 8 ? <p className="bx-muted">and {locations.length - 8} more</p> : null}
        </>
      ) : null}

      {d.evidence.length > 0 ? (
        <>
          <h3 className="bx-sub">Evidence from the log</h3>
          <pre className="bx-evidence">{d.evidence.slice(0, 5).map((e) => e.text).join("\n")}</pre>
        </>
      ) : null}

      {d.remediation ? (
        <>
          <h3 className="bx-sub">How to fix it</h3>
          <p className="bx-fix">{d.remediation.summary}</p>
          {d.remediation.steps.length > 0 ? (
            <ol className="bx-steps">
              {d.remediation.steps.map((s) => <li key={s}>{s}</li>)}
            </ol>
          ) : null}
        </>
      ) : null}

      {d.retrieved_cases.length > 0 ? (
        <>
          <h3 className="bx-sub">Similar past fixes</h3>
          <ul className="bx-cases">
            {d.retrieved_cases.map((c) => (
              <li key={c.case_id}>
                <strong>{pct(c.similarity, 0)} match</strong> {c.fix_summary}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      <p className="bx-meta">Answered in {d.latency_ms} ms · same code as the diagnosis worker</p>
    </section>
  );
}

export default function BuildExpertPage() {
  const [apiState, setApiState] = useState<ApiState>("checking");
  const [model, setModel] = useState<ModelSummary | null>(null);
  const [examples, setExamples] = useState<Example[]>([]);
  const [mode, setMode] = useState<Mode>("predict");
  const [selected, setSelected] = useState<Record<Mode, string | null>>({ predict: null, diagnose: null });
  const [uploads, setUploads] = useState<Record<Mode, Input | null>>({ predict: null, diagnose: null });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [risk, setRisk] = useState<{ input: Input; out: RiskOutput } | null>(null);
  const [diag, setDiag] = useState<{ input: Input; d: Diagnosis } | null>(null);

  const connect = useCallback(async () => {
    setApiState("checking");
    try {
      const [m, ex] = await Promise.all([getModel(), getExamples()]);
      setModel(m);
      setExamples(ex);
      setApiState("connected");
      setSelected((s) => ({
        predict: s.predict ?? ex.find((e) => e.kind === "commits")?.name ?? null,
        diagnose: s.diagnose ?? ex.find((e) => e.kind === "failures")?.name ?? null,
      }));
    } catch {
      setApiState("offline");
    }
  }, []);

  useEffect(() => {
    void connect();
  }, [connect]);

  const run = async () => {
    const name = selected[mode];
    if (!name) return;
    setBusy(true);
    setError(null);
    try {
      const upload = uploads[mode];
      let input: Input;
      if (upload && upload.label === name) {
        input = upload;
      } else {
        const example = examples.find((e) => e.kind === KIND[mode] && e.name === name);
        if (!example) throw new Error("Pick an example first.");
        input = { label: example.label, body: await getExample(example), actual: actualFromName(example.name) };
      }
      if (mode === "predict") setRisk({ input, out: await predict(input.body) });
      else setDiag({ input, d: await diagnose(input.body) });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const result = mode === "predict" ? risk : diag;

  return (
    <main className="bx-main">
      <header className="bx-header">
        <div>
          <p className="bx-kicker">C2 · BUILD AND CODE QUALITY EXPERT</p>
          <h1>Will this commit break the build?</h1>
          <p className="bx-description">
            Before CI finishes, the expert gives the chance that a commit fails because of TypeScript, ESLint, Prettier or a bundler, and
            says why. After a failure, it reads the log and says what broke, where, and how to fix it.
          </p>
        </div>
        <button type="button" className={`bx-status ${apiState}`} onClick={() => void connect()} title="Check the connection again">
          <span />
          {apiState === "connected" ? `Connected · ${model?.model_version ?? ""}` : apiState === "checking" ? "Connecting…" : "Expert offline"}
          <RefreshCw size={12} />
        </button>
      </header>

      {apiState === "offline" ? (
        <div className="bx-note warn">
          <AlertTriangle size={15} /> Start the Build expert first, from the repo folder:
          <code>uv run python demo-kit/run_api.py</code>
          then press the status button above.
        </div>
      ) : (
        <ModelStrip model={model} />
      )}

      <div className="bx-tabs" role="tablist" aria-label="What to show">
        <button type="button" role="tab" aria-selected={mode === "predict"} className={mode === "predict" ? "selected" : ""} onClick={() => setMode("predict")}>
          <GitCommitHorizontal size={15} /> Before CI: predict the risk
        </button>
        <button type="button" role="tab" aria-selected={mode === "diagnose"} className={mode === "diagnose" ? "selected" : ""} onClick={() => setMode("diagnose")}>
          <Stethoscope size={15} /> After a failure: diagnose it
        </button>
      </div>

      <div className="bx-workspace">
        <InputPanel
          mode={mode}
          examples={examples}
          selected={selected[mode]}
          onSelect={(name) => setSelected((s) => ({ ...s, [mode]: name }))}
          upload={uploads[mode]}
          onUpload={(input, message) => {
            setError(message ?? null);
            setUploads((u) => ({ ...u, [mode]: input }));
            if (input) setSelected((s) => ({ ...s, [mode]: input.label }));
          }}
          onRun={() => void run()}
          busy={busy}
          apiState={apiState}
        />
        <div className="bx-results">
          {error ? <p className="bx-note warn" role="alert"><AlertTriangle size={15} /> {error}</p> : null}
          {mode === "predict" && risk ? <RiskResult input={risk.input} out={risk.out} /> : null}
          {mode === "diagnose" && diag ? <DiagnosisResult input={diag.input} d={diag.d} /> : null}
          {!result && !error ? (
            <section className="bx-panel bx-empty">
              {mode === "predict" ? <GitCommitHorizontal size={22} /> : <Stethoscope size={22} />}
              <p>
                {mode === "predict"
                  ? "Pick a commit and press Predict. The answer is the same one the team's backend and pre-push hook receive."
                  : "Pick a failed job and press Diagnose. The answer is the same one the diagnosis worker sends back."}
              </p>
            </section>
          ) : null}
        </div>
      </div>

      <footer className="bx-footer">
        Research estimate from a model trained on real GitHub Actions history. Risk factors are patterns the model learned, not proven causes.
      </footer>
    </main>
  );
}
