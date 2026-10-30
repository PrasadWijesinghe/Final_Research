import { Timer } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import type { Diagnosis, ReviewDecision } from "../types/api";

type Decision = ReviewDecision["decision"];

const CHOICES: { value: Decision; label: string }[] = [
  { value: "accept", label: "Accept" },
  { value: "reject", label: "Reject" },
  { value: "modify", label: "Modify" },
  { value: "request_revision", label: "Request revision" },
];

/** Seconds since this component was first shown (the decision timer). */
export function useDecisionTimer(): { seconds: () => number; shown: number } {
  const start = useRef<number | null>(null);
  if (start.current === null) start.current = Date.now();
  const [shown, setShown] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setShown(Math.floor((Date.now() - (start.current ?? Date.now())) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, []);
  return { seconds: () => Math.max(0, Math.floor((Date.now() - (start.current ?? Date.now())) / 1000)), shown };
}

export const fmtClock = (s: number): string => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

export function ReviewForm({
  diagnosis,
  reviewerHash,
  onSaved,
}: {
  diagnosis: Diagnosis;
  reviewerHash: string;
  onSaved?: () => void;
}) {
  const timer = useDecisionTimer();
  const [decision, setDecision] = useState<Decision>("accept");
  const [causeConfirmed, setCauseConfirmed] = useState(true);
  const [comment, setComment] = useState("");
  const [summary, setSummary] = useState(diagnosis.remediation?.summary ?? "");
  const [steps, setSteps] = useState((diagnosis.remediation?.steps ?? []).join("\n"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const pick = (d: Decision) => {
    setDecision(d);
    if (d === "reject") setCauseConfirmed(false); // contract: a rejected diagnosis has no confirmed cause
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (decision === "modify" && !summary.trim()) return setError("Give the corrected remediation summary.");
    if (decision === "request_revision" && !comment.trim()) return setError("Say what the expert should revise.");
    const payload: Partial<ReviewDecision> = {
      diagnosis_id: diagnosis.diagnosis_id,
      decision,
      reviewer_hash: reviewerHash,
      cause_confirmed: decision === "reject" ? false : causeConfirmed,
      seconds_to_decide: timer.seconds(), // decision timer: contract field ReviewDecision.seconds_to_decide
      ...(comment.trim() ? { comment: comment.trim() } : {}),
      ...(decision === "modify"
        ? {
            modified_remediation: {
              summary: summary.trim(),
              steps: steps.split("\n").map((s) => s.trim()).filter(Boolean),
              cites_case_ids: diagnosis.remediation?.cites_case_ids ?? [],
            },
          }
        : {}),
    };
    setBusy(true);
    try {
      const ack = await api.review(payload);
      setDone(ack.forwarded ? "Saved and sent to the expert." : "Saved. The expert could not be reached, so it has not seen it yet.");
      onSaved?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (done) return <p className="pq-ok" role="status">{done}</p>;

  return (
    <form className="pq-review" onSubmit={(e) => void submit(e)} aria-label="Review this diagnosis">
      <div className="pq-review-head">
        <strong>Your review</strong>
        <span className="pq-timer" aria-label="Decision timer"><Timer size={14} /> <span data-testid="timer">{fmtClock(timer.shown)}</span></span>
      </div>
      <div className="pq-seg" role="radiogroup" aria-label="Decision">
        {CHOICES.map((c) => (
          <label key={c.value} className={decision === c.value ? "on" : ""}>
            <input type="radio" name={`d-${diagnosis.diagnosis_id}`} checked={decision === c.value} onChange={() => pick(c.value)} />
            {c.label}
          </label>
        ))}
      </div>
      {decision !== "reject" && (
        <label className="pq-check">
          <input type="checkbox" checked={causeConfirmed} onChange={(e) => setCauseConfirmed(e.target.checked)} />
          I agree with the root cause
        </label>
      )}
      {decision === "modify" && (
        <>
          <label>Corrected remediation summary
            <input value={summary} maxLength={500} onChange={(e) => setSummary(e.target.value)} />
          </label>
          <label>Steps (one per line)
            <textarea rows={3} value={steps} onChange={(e) => setSteps(e.target.value)} />
          </label>
        </>
      )}
      <label>{decision === "request_revision" ? "What should the expert revise?" : "Comment (optional)"}
        <textarea rows={2} maxLength={2000} value={comment} onChange={(e) => setComment(e.target.value)} />
      </label>
      {error && <p className="pq-err" role="alert">{error}</p>}
      <button className="pq-btn pq-primary" type="submit" disabled={busy}>{busy ? "Sending..." : "Submit review"}</button>
    </form>
  );
}
