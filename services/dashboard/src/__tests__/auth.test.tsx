import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";

afterEach(() => vi.unstubAllGlobals());

describe("App auth gate", () => {
  it("401 from /auth/me shows the GitHub sign-in button on protected pages", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 401 })));
    window.location.hash = "#/";
    render(<App />);
    expect(await screen.findByText("Sign in with GitHub")).toHaveAttribute("href", "/api/auth/github/login");
  });
  it("API down shows an error, not a blank page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("x")));
    window.location.hash = "#/history";
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the API");
  });
  it("signed-in header shows login", async () => {
    vi.stubGlobal("fetch", vi.fn((u: string) =>
      Promise.resolve(new Response(JSON.stringify(u === "/api/auth/me" ? { login: "octo", avatar_url: "x", user_hash: "a".repeat(64) } : []), { status: 200 }))));
    window.location.hash = "#/failures";
    render(<App />);
    expect(await screen.findByText("octo")).toBeInTheDocument();
    expect(await screen.findByText("No failures recorded")).toBeInTheDocument();
  });
});
