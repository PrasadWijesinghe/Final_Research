import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";
import type { Me } from "../types/api";
import { api, ApiError, setUnauthorizedHandler } from "./api";

export type AuthState =
  | { status: "loading" }
  | { status: "anon" }
  | { status: "error"; message: string }
  | { status: "authed"; me: Me };

interface AuthCtx {
  auth: AuthState;
  refresh: () => void;
  signOut: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [auth, setAuth] = useState<AuthState>({ status: "loading" });

  const refresh = useCallback(() => {
    setAuth({ status: "loading" });
    api.me().then(
      (me) => setAuth({ status: "authed", me }),
      (e: unknown) => {
        if (e instanceof ApiError && e.status === 401) setAuth({ status: "anon" });
        else setAuth({ status: "error", message: e instanceof Error ? e.message : String(e) });
      },
    );
  }, []);

  useEffect(() => {
    refresh();
    setUnauthorizedHandler(() => setAuth({ status: "anon" }));
    return () => setUnauthorizedHandler(null);
  }, [refresh]);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setAuth({ status: "anon" });
    }
  }, []);

  return <Ctx.Provider value={{ auth, refresh, signOut }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useAuth needs AuthProvider");
  return c;
}
