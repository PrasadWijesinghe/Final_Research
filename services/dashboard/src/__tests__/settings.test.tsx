import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Settings from "../pages/Settings";

const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status });

afterEach(() => vi.unstubAllGlobals());

describe("Settings", () => {
  it("shows the new key once, with a warning, and not after Done", async () => {
    const f = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/me/api-keys" && init?.method === "POST") return Promise.resolve(json({ id: "k2", key: "piq_secret_value", created_at: "2026-10-28T00:00:00Z" }, 201));
      return Promise.resolve(json([{ id: "k1", created_at: "2026-10-01T00:00:00Z", revoked_at: null }]));
    });
    vi.stubGlobal("fetch", f);
    render(<Settings />);
    await screen.findByText("k1");
    fireEvent.click(screen.getByText("Create API key"));
    expect(await screen.findByTestId("new-key")).toHaveTextContent("piq_secret_value");
    expect(screen.getByText(/shown only once/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Done"));
    expect(screen.queryByText("piq_secret_value")).not.toBeInTheDocument();
    // the list refetch never contains the plain key
    await waitFor(() => expect(f.mock.calls.filter(([u, i]) => u === "/api/me/api-keys" && !i?.method).length).toBeGreaterThan(1));
  });

  it("shows an error when the API is down", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("x")));
    render(<Settings />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the API");
  });
});
