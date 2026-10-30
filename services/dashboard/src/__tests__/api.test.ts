import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, request, setUnauthorizedHandler } from "../lib/api";

const respond = (status: number, body?: unknown) =>
  vi.fn().mockResolvedValue(new Response(body === undefined ? null : JSON.stringify(body), { status }));

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe("api client", () => {
  it("calls /api with credentials", async () => {
    const f = respond(200, []);
    vi.stubGlobal("fetch", f);
    await api.runs("o/r", 10);
    expect(f).toHaveBeenCalledWith("/api/runs?repo=o%2Fr&limit=10", expect.objectContaining({ credentials: "include" }));
  });
  it("401 throws ApiError and fires the unauthorized handler", async () => {
    vi.stubGlobal("fetch", respond(401, { detail: "no" }));
    const h = vi.fn();
    setUnauthorizedHandler(h);
    await expect(request("/runs")).rejects.toMatchObject({ status: 401 });
    expect(h).toHaveBeenCalledOnce();
  });
  it("401 from /auth/me does not fire the handler", async () => {
    vi.stubGlobal("fetch", respond(401));
    const h = vi.fn();
    setUnauthorizedHandler(h);
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(h).not.toHaveBeenCalled();
  });
  it("204 returns undefined", async () => {
    vi.stubGlobal("fetch", respond(204));
    await expect(api.logout()).resolves.toBeUndefined();
  });
  it("network failure becomes status 0", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("down")));
    await expect(request("/runs")).rejects.toMatchObject({ status: 0 });
  });
  it("server detail is surfaced", async () => {
    vi.stubGlobal("fetch", respond(404, { detail: "diagnosis not found" }));
    await expect(request("/x")).rejects.toThrow("diagnosis not found");
  });
});
