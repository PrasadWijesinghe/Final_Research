import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./api";

export type AsyncState<T> =
  | { status: "loading" }
  | { status: "error"; error: ApiError }
  | { status: "ready"; data: T };

/** Run an API call on mount (and when `deps` change); `reload` runs it again. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): [AsyncState<T>, () => void] {
  const [state, setState] = useState<AsyncState<T>>({ status: "loading" });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setState({ status: "loading" });
    fn().then(
      (data) => live && setState({ status: "ready", data }),
      (e: unknown) =>
        live &&
        setState({ status: "error", error: e instanceof ApiError ? e : new ApiError(0, String(e)) }),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return [state, reload];
}
