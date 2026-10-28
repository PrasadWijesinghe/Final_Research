import { Activity, LogOut } from "lucide-react";
import type { ReactNode } from "react";
import { useAuth } from "../lib/auth";
import { Link, type Route } from "../lib/router";
import { LOGIN_URL } from "../lib/api";

const NAV: { to: string; label: string; names: Route["name"][] }[] = [
  { to: "/", label: "Risk", names: ["dashboard", "commit"] },
  { to: "/history", label: "History", names: ["history"] },
  { to: "/failures", label: "Failures", names: ["failures", "failure"] },
  { to: "/settings", label: "Settings", names: ["settings"] },
  { to: "/infra", label: "Infra demo", names: ["infra"] },
];

export function Shell({ route, children }: { route: Route; children: ReactNode }) {
  const { auth, signOut } = useAuth();
  return (
    <div className="pq-shell">
      <nav className="pq-nav" aria-label="Main">
        <Link to="/" className="pq-brand">
          <span className="brand-mark pq-mark">
            <Activity size={16} />
          </span>
          PipelineIQ
        </Link>
        <div className="pq-links">
          {NAV.map((n) => (
            <Link key={n.to} to={n.to} aria-current={n.names.includes(route.name) ? "page" : undefined}>
              {n.label}
            </Link>
          ))}
        </div>
        <div className="pq-user">
          {auth.status === "authed" ? (
            <>
              <img src={auth.me.avatar_url} alt="" width={24} height={24} />
              <span>{auth.me.login}</span>
              <button className="pq-btn pq-ghost" onClick={() => void signOut()} title="Sign out">
                <LogOut size={14} /> Sign out
              </button>
            </>
          ) : auth.status === "anon" ? (
            <a className="pq-btn" href={LOGIN_URL}>
              Sign in
            </a>
          ) : null}
        </div>
      </nav>
      <div className="pq-body">{children}</div>
    </div>
  );
}
