import type { Expert, RiskOutput, RunItem } from "../types/api";

export const EXPERTS: Expert[] = ["dependency", "build", "test", "infra"];
export type Level = "high" | "medium" | "low" | "none";

/** Risk level for one prediction. */
export function levelOf(r: RiskOutput): Level {
  if (r.status === "error" || r.probability == null) return "none";
  if (r.alert) return "high";
  // DEFAULT: "medium" = at least half of the alert threshold; the thresholds are the experts' own.
  return r.probability >= r.threshold * 0.5 ? "medium" : "low";
}

/** The riskiest prediction of one expert (the four experts are never combined into one score). */
export function topFor(preds: RiskOutput[], expert: Expert): RiskOutput | null {
  const mine = preds.filter((p) => p.expert === expert && p.probability != null);
  if (mine.length === 0) return preds.find((p) => p.expert === expert) ?? null;
  return mine.reduce((a, b) => ((b.probability ?? 0) > (a.probability ?? 0) ? b : a));
}

export const pct = (p: number | null | undefined): string => (p == null ? "n/a" : `${(p * 100).toFixed(0)}%`);

export const shortSha = (s: string): string => s.slice(0, 7);

export function when(iso: string | null | undefined): string {
  if (!iso) return "n/a";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function alertsOf(run: RunItem): RiskOutput[] {
  return run.predictions.filter((p) => p.alert);
}

export type Outcome = "failed" | "passed" | "pending";
/** Actual outcome of a run. */
export function outcomeOf(run: RunItem): Outcome {
  if (!run.run_conclusion) return "pending";
  return run.run_conclusion === "success" ? "passed" : "failed";
}
