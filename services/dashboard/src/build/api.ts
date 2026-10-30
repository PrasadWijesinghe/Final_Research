// Calls to the Build expert (experts/build), through the dev server's /build-api proxy.
// Types mirror shared/contracts (RiskOutput, Diagnosis) for the fields this page shows.

export const BUILD_API = "/build-api";

export type RiskFactor = {
  feature: string;
  value: string | number | boolean;
  shap_value: number;
  direction: "raises" | "lowers";
  reason: string;
  path: string | null;
  line: number | null;
};

export type RiskOutput = {
  probability: number | null;
  threshold: number;
  alert: boolean;
  status: "ok" | "degraded" | "error";
  error_message: string | null;
  model_version: string;
  sub_risks: Record<string, number>;
  top_factors: RiskFactor[];
  latency_ms: number;
};

export type BuildLocation = { path: string; line: number | null; code: string | null };

export type Diagnosis = {
  status: "diagnosed" | "misrouted" | "error";
  error_message: string | null;
  suggested_category: string | null;
  root_cause: string | null;
  confidence: number | null;
  evidence: { source: string; text: string; line: number | null; path: string | null }[];
  retrieved_cases: { case_id: string; similarity: number; summary: string; fix_summary: string }[];
  remediation: { summary: string; steps: string[] } | null;
  details: { expert: string; tool?: string; locations?: BuildLocation[] } | null;
  latency_ms: number;
};

export type ModelSummary = {
  model_version: string;
  loaded: boolean;
  threshold?: number;
  dataset_id?: string;
  test?: {
    precision: number | null;
    recall: number | null;
    f1: number | null;
    pr_auc: number | null;
    roc_auc: number | null;
    commits: number | null;
    failures: number | null;
  };
  best_baseline?: { description: string | null; f1: number | null; pr_auc: number | null };
};

export type ExampleKind = "commits" | "failures";
export type Example = { kind: ExampleKind; name: string; label: string };

/** A commit or failed job to send: from the kit's examples or an uploaded file. */
export type Input = { label: string; body: unknown; actual: "failed" | "passed" | null };

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BUILD_API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json" },
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) {
    let detail = "";
    try {
      const body = (await response.json()) as { detail?: unknown };
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? "");
    } catch {
      detail = response.statusText;
    }
    throw new Error(`The Build expert answered ${response.status}${detail ? `: ${detail.slice(0, 300)}` : ""}`);
  }
  return (await response.json()) as T;
}

export const getModel = () => call<ModelSummary>("/model");
export const getExamples = () => call<Example[]>("/examples");
export const getExample = (e: Example) => call<unknown>(`/examples/${e.kind}/${encodeURIComponent(e.name)}`);
export const predict = (context: unknown) =>
  call<RiskOutput[]>("/predict", { method: "POST", body: JSON.stringify(context) }).then((r) => r[0]);
export const diagnose = (failure: unknown) =>
  call<Diagnosis>("/diagnose", { method: "POST", body: JSON.stringify(failure) });

/** What really happened, from the kit's file names (`..._failed.json`, `..._passed.json`). */
export function actualFromName(name: string): "failed" | "passed" | null {
  if (name.endsWith("_failed.json")) return "failed";
  if (name.endsWith("_passed.json")) return "passed";
  return null;
}

export const pct = (p: number | null | undefined, digits = 1): string =>
  p == null ? "n/a" : `${(p * 100).toFixed(digits)}%`;

/** The dashboard's risk level: alert = high, at least half the threshold = medium, else low. */
export function levelOf(r: RiskOutput): "high" | "medium" | "low" | "none" {
  if (r.status === "error" || r.probability == null) return "none";
  if (r.alert) return "high";
  return r.probability >= r.threshold * 0.5 ? "medium" : "low";
}
