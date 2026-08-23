import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import FailedAnalysisNotice from "./FailedAnalysisNotice";
import type { Analysis } from "../api/types";

const analysis = (failure_reason: string): Analysis => ({
  id: "a1",
  note_id: "n1",
  user_id: "u1",
  conditions: [],
  gaps: [],
  summary: "failed",
  model_version: "test",
  prompt_version: "v1",
  created_at: "2026-01-01T00:00:00Z",
  is_failed: true,
  failure_reason: failure_reason as Analysis["failure_reason"],
});

describe("FailedAnalysisNotice", () => {
  it.each([
    ["rate_limited", "Analysis temporarily unavailable"],
    ["invalid_output", "Analysis failed"],
    ["timeout", "Analysis timed out"],
    ["provider_error", "Analysis unavailable"],
    ["unknown", "Analysis failed"],
  ])("maps %s to %s", (reason, title) => {
    render(
      <FailedAnalysisNotice
        analysis={analysis(reason)}
        onRetry={() => undefined}
        retrying={false}
      />,
    );

    expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry analysis" })).toBeInTheDocument();
  });

  it("calls onRetry when the Retry analysis button is clicked", async () => {
    const onRetry = vi.fn();
    render(
      <FailedAnalysisNotice
        analysis={analysis("timeout")}
        onRetry={onRetry}
        retrying={false}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Retry analysis" }));

    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("disables the Retry button while retrying", () => {
    render(
      <FailedAnalysisNotice
        analysis={analysis("timeout")}
        onRetry={vi.fn()}
        retrying={true}
      />,
    );

    expect(screen.getByRole("button", { name: "Retrying…" })).toBeDisabled();
  });

  it("renders the failure message for a known failure reason", () => {
    render(
      <FailedAnalysisNotice
        analysis={analysis("timeout")}
        onRetry={vi.fn()}
        retrying={false}
      />,
    );

    expect(
      screen.getByText("The AI service took too long to respond. Please try again."),
    ).toBeInTheDocument();
  });
});