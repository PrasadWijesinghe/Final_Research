import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReviewForm } from "../components/ReviewForm";
import type { Diagnosis } from "../types/api";

const diagnosis = {
  diagnosis_id: "11111111-1111-4111-8111-111111111111",
  failure_id: "22222222-2222-4222-8222-222222222222",
  expert: "test",
  status: "diagnosed",
  root_cause: "flaky timer",
  confidence: 0.8,
  latency_ms: 5,
  remediation: { summary: "use fake timers", steps: ["a"], cites_case_ids: [] },
} as unknown as Diagnosis;

const HASH = "a".repeat(64);
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ decision_id: "d", forwarded: true }), { status: 201 }));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const sent = () => JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);

describe("ReviewForm", () => {
  it("runs a visible timer and sends seconds_to_decide", async () => {
    render(<ReviewForm diagnosis={diagnosis} reviewerHash={HASH} />);
    expect(screen.getByTestId("timer")).toHaveTextContent("00:00");
    act(() => { vi.advanceTimersByTime(7000); });
    expect(screen.getByTestId("timer")).toHaveTextContent("00:07");
    fireEvent.click(screen.getByText("Submit review"));
    await act(async () => { await Promise.resolve(); });
    const body = sent();
    expect(body).toMatchObject({ diagnosis_id: diagnosis.diagnosis_id, decision: "accept", reviewer_hash: HASH, cause_confirmed: true, seconds_to_decide: 7 });
    expect(body.modified_remediation).toBeUndefined();
  });

  it("reject forces cause_confirmed false", async () => {
    render(<ReviewForm diagnosis={diagnosis} reviewerHash={HASH} />);
    fireEvent.click(screen.getByLabelText("Reject"));
    fireEvent.click(screen.getByText("Submit review"));
    await act(async () => { await Promise.resolve(); });
    expect(sent()).toMatchObject({ decision: "reject", cause_confirmed: false });
  });

  it("modify sends modified_remediation", async () => {
    render(<ReviewForm diagnosis={diagnosis} reviewerHash={HASH} />);
    fireEvent.click(screen.getByLabelText("Modify"));
    fireEvent.change(screen.getByLabelText("Corrected remediation summary"), { target: { value: "mock Date" } });
    fireEvent.change(screen.getByLabelText(/Steps/), { target: { value: "one\n\ntwo" } });
    fireEvent.click(screen.getByText("Submit review"));
    await act(async () => { await Promise.resolve(); });
    expect(sent().modified_remediation).toEqual({ summary: "mock Date", steps: ["one", "two"], cites_case_ids: [] });
  });

  it("request revision needs a note and does not call the API without it", async () => {
    render(<ReviewForm diagnosis={diagnosis} reviewerHash={HASH} />);
    fireEvent.click(screen.getByLabelText("Request revision"));
    fireEvent.click(screen.getByText("Submit review"));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("alert")).toHaveTextContent("revise");
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("What should the expert revise?"), { target: { value: "check logs" } });
    fireEvent.click(screen.getByText("Submit review"));
    await act(async () => { await Promise.resolve(); });
    expect(sent()).toMatchObject({ decision: "request_revision", comment: "check logs" });
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
  });
});
