import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
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
});