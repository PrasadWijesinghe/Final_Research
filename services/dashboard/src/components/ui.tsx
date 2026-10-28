import { AlertTriangle, Inbox, LoaderCircle } from "lucide-react";
import type { ReactNode } from "react";
import type { AsyncState } from "../lib/useAsync";
import { type Level, pct } from "../lib/risk";
import type { RiskOutput } from "../types/api";

export function Loading({ what = "Loading" }: { what?: string }) {
  return (
    <div className="pq-state" role="status">
      <LoaderCircle className="spin" size={20} /> {what}...
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="pq-state">
      <Inbox size={20} />
      <div>
        <strong>{title}</strong>
        {hint && <p>{hint}</p>}
      </div>
    </div>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="pq-state pq-error" role="alert">
      <AlertTriangle size={20} />
      <div>
        <strong>Something went wrong</strong>
        <p>{message}</p>
      </div>
      {onRetry && (
        <button className="pq-btn" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** Shows loading / error / empty / data for one API call. */
export function AsyncView<T>({
  state,
  reload,
  what,
  isEmpty,
  empty,
  children,
}: {
  state: AsyncState<T>;
  reload: () => void;
  what?: string;
  isEmpty?: (d: T) => boolean;
  empty?: ReactNode;
  children: (d: T) => ReactNode;
}) {
  if (state.status === "loading") return <Loading what={what} />;
  if (state.status === "error") return <ErrorBox message={state.error.message} onRetry={reload} />;
  if (isEmpty?.(state.data)) return <>{empty}</>;
  return <>{children(state.data)}</>;
}

export function Page({ title, kicker, actions, children }: { title: string; kicker?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="pq-page">
      <header className="pq-page-head">
        <div>
          {kicker && <p className="eyebrow">{kicker}</p>}
          <h2>{title}</h2>
        </div>
        {actions}
      </header>
      {children}
    </section>
  );
}

export function LevelBadge({ level, children }: { level: Level; children: ReactNode }) {
  return <span className={`pq-badge pq-${level}`}>{children}</span>;
}

/** One expert's risk as "level + percentage"; a dash when the expert gave no answer. */
export function RiskCell({ risk }: { risk: RiskOutput | null }) {
  if (!risk) return <span className="pq-dim">no data</span>;
  if (risk.status === "error" || risk.probability == null) return <LevelBadge level="none">error</LevelBadge>;
  const level = risk.alert ? "high" : risk.probability >= risk.threshold * 0.5 ? "medium" : "low";
  return (
    <LevelBadge level={level}>
      {level} {pct(risk.probability)}
    </LevelBadge>
  );
}
