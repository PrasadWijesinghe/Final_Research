import { type AnchorHTMLAttributes, useEffect, useState } from "react";

export type Route =
  | { name: "dashboard" }
  | { name: "history" }
  | { name: "commit"; sha: string }
  | { name: "failures" }
  | { name: "failure"; id: string }
  | { name: "settings" }
  | { name: "infra" }
  | { name: "signin" }
  | { name: "notfound"; path: string };

/** Turn a location hash like "#/commits/abc?repo=o/r" into a route. */
export function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, "").split("?")[0].replace(/\/+$/, "") || "/";
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts.length === 0) return { name: "dashboard" };
  const [a, b] = parts;
  if (parts.length === 1) {
    if (a === "history") return { name: "history" };
    if (a === "failures") return { name: "failures" };
    if (a === "settings") return { name: "settings" };
    if (a === "infra") return { name: "infra" };
    if (a === "signin") return { name: "signin" };
  }
  if (parts.length === 2 && a === "commits") return { name: "commit", sha: b };
  if (parts.length === 2 && a === "failures") return { name: "failure", id: b };
  return { name: "notfound", path };
}

/** Query string of the current hash, e.g. "?repo=o/r". */
export function hashQuery(hash: string): URLSearchParams {
  const i = hash.indexOf("?");
  return new URLSearchParams(i >= 0 ? hash.slice(i + 1) : "");
}

export function useHash(): string {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export function useRoute(): Route {
  return parseHash(useHash());
}

export function Link({ to, ...rest }: { to: string } & AnchorHTMLAttributes<HTMLAnchorElement>) {
  return <a href={`#${to}`} {...rest} />;
}
