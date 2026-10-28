import {
  AlertTriangle,
  ArrowDownToLine,
  ArrowRight,
  Check,
  CheckCircle2,
  Circle,
  Clipboard,
  FileText,
  GitBranch,
  Link2,
  LoaderCircle,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  Upload,
} from "lucide-react";
import { useEffect, useRef, useState, type DragEvent, type FormEvent } from "react";
import { dependencyMockCases } from "./mockData";
import type { DependencyAnalysisResult, DiagnosticSignal } from "./types";
import "./dependency-dashboard.css";

type InputMode = "url" | "upload" | "paste";
type AnalysisPayload = { log_text: string } | { run_url: string };
type ResultSource = "sample" | "live" | "offline";

class ApiRequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

function AnalysisInputPanel({
  selectedSample,
  onSelectSample,
  onPreviewSample,
  onAnalyzeInput,
  isAnalyzing,
}: {
  selectedSample: string;
  onSelectSample: (sampleId: string) => void;
  onPreviewSample: () => void;
  onAnalyzeInput: (payload: AnalysisPayload) => void;
  isAnalyzing: boolean;
}) {
  const [mode, setMode] = useState<InputMode>("url");
  const [runUrl, setRunUrl] = useState("");
  const [logText, setLogText] = useState("");
  const [fileName, setFileName] = useState("");
  const [fileError, setFileError] = useState("");
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const readLogFile = async (file?: File) => {
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      setFileError("This prototype accepts log files up to 5 MB.");
      return;
    }
    if (!/\.(log|txt)$/i.test(file.name)) {
      setFileError("Choose a .log or .txt file.");
      return;
    }
    setFileError("");
    setFileName(file.name);
    setLogText(await file.text());
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    void readLogFile(event.dataTransfer.files[0]);
  };

  const submitInput = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFileError("");
    if (mode === "url") {
      if (!runUrl.trim()) {
        setFileError("Enter a GitHub Actions run URL.");
        return;
      }
      onAnalyzeInput({ run_url: runUrl.trim() });
      return;
    }
    if (!logText.trim()) {
      setFileError("Add log text before analyzing.");
      return;
    }
    onAnalyzeInput({ log_text: logText });
  };

  return (
    <section className="dep-panel dep-input-panel" aria-labelledby="dep-input-title">
      <div className="dep-panel-heading">
        <div>
          <p className="dep-step">01 / Input</p>
          <h2 id="dep-input-title">Inspect a failed run</h2>
        </div>
        <div className="dep-heading-icon"><FileText size={19} /></div>
      </div>

      <div className="dep-input-tabs" role="tablist" aria-label="Log input method">
        <button type="button" role="tab" aria-selected={mode === "url"} className={mode === "url" ? "selected" : ""} onClick={() => setMode("url")}>
          <Link2 size={15} /> Run URL
        </button>
        <button type="button" role="tab" aria-selected={mode === "upload"} className={mode === "upload" ? "selected" : ""} onClick={() => setMode("upload")}>
          <Upload size={15} /> Upload log
        </button>
        <button type="button" role="tab" aria-selected={mode === "paste"} className={mode === "paste" ? "selected" : ""} onClick={() => setMode("paste")}>
          <Clipboard size={15} /> Paste text
        </button>
      </div>

      <form onSubmit={submitInput}>
        {mode === "url" ? (
          <label className="dep-field-label" htmlFor="run-url">
            GitHub Actions run URL
            <span className="dep-url-field">
              <Link2 size={16} aria-hidden="true" />
              <input id="run-url" value={runUrl} onChange={(event) => setRunUrl(event.target.value)} placeholder="https://github.com/org/repo/actions/runs/…" />
            </span>
          </label>
        ) : null}

        {mode === "upload" ? (
          <div
            className={`dep-dropzone ${dragging ? "dragging" : ""}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={handleDrop}
          >
            <input ref={fileInput} className="dep-file-input" type="file" accept=".log,.txt,text/plain" onChange={(event) => void readLogFile(event.target.files?.[0])} aria-label="Choose a sanitized log file" />
            <div className="dep-upload-icon"><Upload size={20} /></div>
            <strong>{fileName || "Drop a sanitized log file here"}</strong>
            <span>{fileName ? `${logText.length.toLocaleString()} characters loaded` : ".log or .txt · up to 5 MB"}</span>
            <button type="button" className="dep-secondary-button" onClick={() => fileInput.current?.click()}>
              Browse files
            </button>
          </div>
        ) : null}

        {mode === "paste" ? (
          <label className="dep-field-label" htmlFor="pasted-log">
            Sanitized job log
            <textarea id="pasted-log" className="dep-paste-area" value={logText} onChange={(event) => setLogText(event.target.value)} placeholder={'Paste relevant job log lines…\n\nRemove tokens, secrets, and personal data before pasting.'} />
            <span className="dep-character-count">{logText.length.toLocaleString()} characters</span>
          </label>
        ) : null}
        {fileError ? <p className="dep-field-error" role="alert">{fileError}</p> : null}
        <button className="dep-primary-button dep-input-submit" type="submit" disabled={isAnalyzing}>
          {isAnalyzing ? <LoaderCircle className="dep-spin" size={16} /> : <Sparkles size={16} />}
          {isAnalyzing ? "Analyzing log..." : mode === "url" ? "Analyze GitHub run" : "Analyze log"}
        </button>
      </form>

      <div className="dep-samples">
        <div className="dep-samples-heading">
          <span><Sparkles size={15} /> Try a sample log</span>
          <span className="dep-mock-tag">MOCK DATA</span>
        </div>
        <div className="dep-sample-options">
          {dependencyMockCases.map((sample) => (
            <button key={sample.id} type="button" disabled={isAnalyzing} className={`dep-sample-option ${selectedSample === sample.id ? "selected" : ""}`} onClick={() => onSelectSample(sample.id)}>
              <span className="dep-sample-radio">{selectedSample === sample.id ? <Check size={12} /> : null}</span>
              <span><strong>{sample.caseName}</strong><small>{sample.repository}</small></span>
              <ArrowRight size={15} />
            </button>
          ))}
        </div>
        <button className="dep-primary-button" type="button" disabled={!selectedSample || isAnalyzing} onClick={onPreviewSample}>
          {isAnalyzing ? <LoaderCircle className="dep-spin" size={17} /> : <Sparkles size={17} />} Analyze selected sample
        </button>
        <p className="dep-preview-note">Samples are sent to the local API as demo logs. If it is unavailable, the selected mock result is shown and clearly marked.</p>
      </div>
    </section>
  );
}

function ClassificationResultCard({ result, source }: { result: DependencyAnalysisResult; source: ResultSource }) {
  const isDependency = result.prediction === "DEPENDENCY_FAILURE";
  const sourceLabel = source === "live" ? "LIVE MODEL" : source === "offline" ? "OFFLINE / MOCK RESULT" : "MOCK SAMPLE";
  return (
    <section className={`dep-result-card ${isDependency ? "dependency" : "other"}`} aria-live="polite" aria-labelledby="dep-result-title">
      <div className="dep-result-topline">
        <span className="dep-result-eyebrow"><span className="dep-live-dot" /> CLASSIFICATION RESULT</span>
        <span className={`dep-source-badge ${source}`}>{sourceLabel}</span>
      </div>
      <div className="dep-result-main">
        <div className={`dep-result-icon ${isDependency ? "dependency" : "other"}`}>
          {isDependency ? <AlertTriangle size={22} /> : <CheckCircle2 size={22} />}
        </div>
        <div className="dep-result-title-wrap">
          <p className="dep-step">{result.repository} <span>·</span> {result.runId}</p>
          <h2 id="dep-result-title">{result.prediction.replaceAll("_", " ")}</h2>
        </div>
        <div className="dep-confidence">
          <strong>{(result.confidence * 100).toFixed(1)}<small>%</small></strong>
          <span>{source === "live" ? "Model confidence" : "Mock confidence"}</span>
        </div>
      </div>
      <div className="dep-confidence-track" aria-label={`Mock confidence ${(result.confidence * 100).toFixed(1)} percent`}>
        <span style={{ width: `${result.confidence * 100}%` }} />
      </div>
      <div className="dep-research-caveat"><AlertTriangle size={15} /><span>{source === "live" ? "Research estimate from the log-only model; exploratory performance is based on repository-held-out evaluation." : source === "offline" ? "Backend unavailable. This is a static mock result, not a prediction for the submitted log." : "Static sample preview. This is illustrative and is not a live model prediction."}</span></div>
    </section>
  );
}

function DiagnosticSignalChip({ signal }: { signal: DiagnosticSignal }) {
  return (
    <div className={`dep-signal-chip ${signal.detected ? "detected" : "not-detected"}`} title={signal.description}>
      <span className="dep-signal-status">{signal.detected ? <Check size={13} /> : <Circle size={11} />}</span>
      <span>{signal.label}</span>
      <span className="dep-signal-state">{signal.detected ? "Detected" : "Not detected"}</span>
    </div>
  );
}

function DiagnosticSignalsPanel({ signals }: { signals: DiagnosticSignal[] }) {
  const detectedCount = signals.filter((signal) => signal.detected).length;
  return (
    <section className="dep-panel dep-signals-panel" aria-labelledby="dep-signals-title">
      <div className="dep-panel-heading compact">
        <div><p className="dep-step">02 / Model indicators</p><h2 id="dep-signals-title">Diagnostic signals</h2></div>
        <span className="dep-count-badge">{detectedCount} / {signals.length} detected</span>
      </div>
      <div className="dep-signals-list">{signals.map((signal) => <DiagnosticSignalChip key={signal.id} signal={signal} />)}</div>
      <p className="dep-panel-footnote">Signals are log-derived indicators; they describe evidence found in the sample, not independent proof of cause.</p>
    </section>
  );
}

function LogEvidenceViewer({ evidence }: { evidence: DependencyAnalysisResult["evidence"] }) {
  const lines = evidence.excerpt.split("\n");
  return (
    <section className="dep-panel dep-evidence-panel" aria-labelledby="dep-evidence-title">
      <div className="dep-panel-heading compact">
        <div><p className="dep-step">03 / Sanitized evidence</p><h2 id="dep-evidence-title">{evidence.title}</h2></div>
        <button type="button" className="dep-icon-button" aria-label="Download evidence (preview only)" title="Evidence preview only"><ArrowDownToLine size={17} /></button>
      </div>
      <div className="dep-log-window">
        <div className="dep-log-toolbar"><span><i /> <i /> <i /></span><code>{evidence.fileName}</code><span className="dep-sanitized-label">SANITIZED</span></div>
        <div className="dep-log-lines" role="region" aria-label="Sanitized log excerpt" tabIndex={0}>
          {lines.map((line, index) => {
            const lineNumber = evidence.startLine + index;
            const highlighted = evidence.highlightedLines.includes(index + 1);
            return <div key={`${lineNumber}-${index}`} className={`dep-log-line ${highlighted ? "highlighted" : ""}`}><span className="dep-line-number">{lineNumber}</span><code>{line || " "}</code></div>;
          })}
        </div>
      </div>
      <p className="dep-panel-footnote">Highlighted lines illustrate the evidence display. In the live flow, excerpts must come from sanitized job logs.</p>
    </section>
  );
}

function RemediationCard({ recommendation }: { recommendation: DependencyAnalysisResult["recommendation"] }) {
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);
  const [confirmationVisible, setConfirmationVisible] = useState(false);
  const confirmationTimer = useRef<number | null>(null);

  useEffect(() => () => {
    if (confirmationTimer.current !== null) window.clearTimeout(confirmationTimer.current);
  }, []);

  const recordFeedback = (choice: "up" | "down") => {
    setFeedback(choice);
    setConfirmationVisible(true);
    if (confirmationTimer.current !== null) window.clearTimeout(confirmationTimer.current);
    confirmationTimer.current = window.setTimeout(() => setConfirmationVisible(false), 3200);
  };

  return (
    <section className="dep-panel dep-remediation-panel" aria-labelledby="dep-remediation-title">
      <div className="dep-panel-heading compact">
        <div><p className="dep-step">04 / Suggested action</p><h2 id="dep-remediation-title">Remediation insights</h2></div>
        <div className="dep-heading-icon warm"><Sparkles size={18} /></div>
      </div>
      <p className="dep-remediation-summary">{recommendation.summary}</p>
      <ol className="dep-remediation-steps">
        {recommendation.steps.map((step, index) => <li key={step}><span>{String(index + 1).padStart(2, "0")}</span><p>{step}</p></li>)}
      </ol>
      <div className="dep-remediation-disclaimer"><AlertTriangle size={14} /><span>Suggested steps are research guidance. Verify them against your repository and package documentation.</span></div>
      <div className="dep-feedback" aria-label="Diagnostic feedback">
        <span className="dep-feedback-question">Was this diagnostic accurate?</span>
        <div className="dep-feedback-actions">
          <button type="button" className={feedback === "up" ? "selected" : ""} aria-label="Yes, this diagnostic was accurate" aria-pressed={feedback === "up"} onClick={() => recordFeedback("up")}><ThumbsUp size={15} /> Helpful</button>
          <button type="button" className={feedback === "down" ? "selected" : ""} aria-label="No, this diagnostic was inaccurate" aria-pressed={feedback === "down"} onClick={() => recordFeedback("down")}><ThumbsDown size={15} /> Inaccurate</button>
        </div>
        {confirmationVisible ? <span className="dep-feedback-confirmation" role="status"><CheckCircle2 size={14} /> Feedback recorded for model review</span> : null}
      </div>
    </section>
  );
}

function DependencyDashboardPage() {
  const [selectedSampleId, setSelectedSampleId] = useState(dependencyMockCases[0]?.id ?? "");
  const [result, setResult] = useState<DependencyAnalysisResult | null>(dependencyMockCases[0] ?? null);
  const [resultSource, setResultSource] = useState<ResultSource>("sample");
  const [previewing, setPreviewing] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const activeRequest = useRef(0);

  const analyze = async (
    payload: AnalysisPayload,
    fallback: DependencyAnalysisResult | null = dependencyMockCases.find((item) => item.id === selectedSampleId) ?? null,
  ) => {
    const requestId = activeRequest.current + 1;
    activeRequest.current = requestId;
    setPreviewing(true);
    setRequestError(null);
    try {
      const response = await fetch("http://127.0.0.1:8001/api/v1/predict/dependency", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({})) as { detail?: unknown };
        const detail = typeof body.detail === "string" ? body.detail : `Prediction API returned HTTP ${response.status}.`;
        throw new ApiRequestError(detail, response.status);
      }
      const prediction = await response.json() as DependencyAnalysisResult;
      if (requestId !== activeRequest.current) return;
      setResult(prediction);
      setResultSource("live");
    } catch (error) {
      if (requestId !== activeRequest.current) return;
      if (error instanceof ApiRequestError && error.status < 500) {
        setRequestError(error.message);
      } else if (fallback) {
        setResult(fallback);
        setResultSource("offline");
      } else {
        setRequestError("The prediction API is unavailable, and there is no sample fallback for this input.");
      }
    } finally {
      if (requestId === activeRequest.current) setPreviewing(false);
    }
  };

  const previewSample = (sampleId = selectedSampleId) => {
    const sample = dependencyMockCases.find((item) => item.id === sampleId);
    if (!sample) return;
    setSelectedSampleId(sampleId);
    void analyze({ log_text: sample.evidence.excerpt }, sample);
  };

  return (
    <main className="dep-main">
      <header className="dep-page-header">
        <div>
          <p className="dep-page-kicker"><GitBranch size={14} /> DEPENDENCY FAILURE EXPERT</p>
          <h1>Understand why CI failed.</h1>
          <p className="dep-page-description">Inspect sanitized GitHub Actions logs for dependency-related failure signals and actionable next steps.</p>
        </div>
        <div className={`dep-status-pill ${resultSource === "live" ? "connected" : resultSource === "offline" ? "offline" : ""}`}><span /> {resultSource === "live" ? "Live model result" : resultSource === "offline" ? "Offline / mock fallback" : "Research demo"}</div>
      </header>

      <div className="dep-workspace">
        <div className="dep-workspace-input"><AnalysisInputPanel selectedSample={selectedSampleId} onSelectSample={(sampleId) => previewSample(sampleId)} onPreviewSample={() => previewSample()} onAnalyzeInput={(payload) => { void analyze(payload); }} isAnalyzing={previewing} /></div>
        <div className="dep-workspace-results">
          {requestError ? <div className="dep-request-error" role="alert"><AlertTriangle size={16} /><span>{requestError}</span></div> : null}
          {previewing ? (
            <div className="dep-loading-state" role="status" aria-live="polite"><LoaderCircle className="dep-spin" size={23} /><span>Analyzing log...</span></div>
          ) : result ? (
            <>
              <ClassificationResultCard result={result} source={resultSource} />
              <DiagnosticSignalsPanel signals={result.signals} />
              <LogEvidenceViewer evidence={result.evidence} />
              <RemediationCard recommendation={result.recommendation} />
            </>
          ) : (
            <div className="dep-empty-result"><FileText size={28} /><h2>Choose a sample to preview</h2><p>Select one of the mock logs to see its sample classification and evidence.</p></div>
          )}
        </div>
      </div>
      <footer className="dep-footer"><span>PipelineIQ - Dependency Expert</span><span><AlertTriangle size={13} /> Exploratory model - verify evidence before acting</span></footer>
    </main>
  );
}

export default DependencyDashboardPage;
