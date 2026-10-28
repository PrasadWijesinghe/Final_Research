import type {
  ApiKeyIssued,
  ApiKeyItem,
  CommitRisks,
  FailureItem,
  FailureView,
  Me,
  ReviewAck,
  ReviewDecision,
  RunItem,
} from "../types/api";

/** An API call that did not succeed. status 0 means the API could not be reached at all. */
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

let onUnauthorized: (() => void) | null = null;
/** The auth provider registers here so any 401 flips the app to signed-out. */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      credentials: "include",
      ...init,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
  } catch {
    throw new ApiError(0, "Cannot reach the API. Is it running?");
  }
  if (res.status === 401) {
    // /auth/me answers 401 for "not signed in"; the caller decides what that means.
    if (path !== "/auth/me") onUnauthorized?.();
    throw new ApiError(401, "Not signed in");
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body: keep the generic text */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  try {
    return (await res.json()) as T;
  } catch {
    throw new ApiError(0, "The API sent an unexpected (non-JSON) answer.");
  }
}

const qs = (params: Record<string, string | number | undefined>): string => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  me: () => request<Me>("/auth/me"),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  listKeys: () => request<ApiKeyItem[]>("/me/api-keys"),
  createKey: () => request<ApiKeyIssued>("/me/api-keys", { method: "POST" }),
  revokeKey: (id: string) => request<void>(`/me/api-keys/${encodeURIComponent(id)}`, { method: "DELETE" }),
  runs: (repo?: string, limit = 50) => request<RunItem[]>(`/runs${qs({ repo, limit })}`),
  commitRisks: (sha: string, repo?: string) =>
    request<CommitRisks>(`/commits/${encodeURIComponent(sha)}/risks${qs({ repo })}`),
  failures: (repo?: string, limit = 50) => request<FailureItem[]>(`/failures${qs({ repo, limit })}`),
  failure: (id: string) => request<FailureView>(`/failures/${encodeURIComponent(id)}`),
  review: (decision: Partial<ReviewDecision>) =>
    request<ReviewAck>("/reviews", { method: "POST", body: JSON.stringify(decision) }),
};

export const LOGIN_URL = "/api/auth/github/login";
