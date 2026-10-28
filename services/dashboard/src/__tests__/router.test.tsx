import { render, screen, act } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { hashQuery, parseHash, useRoute } from "../lib/router";

describe("parseHash", () => {
  it.each([
    ["", { name: "dashboard" }],
    ["#/", { name: "dashboard" }],
    ["#/history", { name: "history" }],
    ["#/commits/abc123?repo=o/r", { name: "commit", sha: "abc123" }],
    ["#/failures", { name: "failures" }],
    ["#/failures/f-1", { name: "failure", id: "f-1" }],
    ["#/settings", { name: "settings" }],
    ["#/infra", { name: "infra" }],
    ["#/signin", { name: "signin" }],
    ["#/nope", { name: "notfound", path: "/nope" }],
  ])("%s", (hash, route) => {
    expect(parseHash(hash)).toEqual(route);
  });
  it("reads the query", () => {
    expect(hashQuery("#/commits/a?repo=o%2Fr").get("repo")).toBe("o/r");
  });
});

describe("useRoute", () => {
  it("follows hashchange", () => {
    function Probe() {
      return <p>{useRoute().name}</p>;
    }
    window.location.hash = "#/history";
    render(<Probe />);
    expect(screen.getByText("history")).toBeInTheDocument();
    act(() => {
      window.location.hash = "#/settings";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(screen.getByText("settings")).toBeInTheDocument();
  });
});
