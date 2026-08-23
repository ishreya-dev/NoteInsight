import { render, screen, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AnalysisReview from "./AnalysisReview";
import type { Analysis, Review } from "../api/types";

const condition = (id: string): Analysis["conditions"][number] => ({
  id,
  condition_name: `Condition ${id}`,
  evidence_quote: `quote for ${id}`,
  documentation_status: "ambiguous",
  suggested_icd10: "Z00.0",
  confidence: 0.8,
  quote_verified: true,
});

const analysis = (overrides: Partial<Analysis> = {}): Analysis => ({
  id: "a1",
  note_id: "n1",
  user_id: "u1",
  conditions: [condition("c1")],
  gaps: [{ description: "Gap 1", related_condition: null }],
  summary: "Patient has condition 1.",
  model_version: "test",
  prompt_version: "v1",
  created_at: "2026-01-01T00:00:00Z",
  is_failed: false,
  failure_reason: null,
  ...overrides,
});

describe("AnalysisReview", () => {
  const defaultProps = {
    analysis: analysis(),
    existingReview: null as Review | null,
    onSaved: vi.fn(),
  };

  it("renders the summary section when streaming", () => {
    render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText="Streaming summary text"
        isStreaming={true}
      />,
    );

    expect(screen.getByRole("heading", { name: "Summary" })).toBeInTheDocument();
    expect(screen.getByText("Streaming summary text")).toBeInTheDocument();
  });

  it("shows streaming text inside the existing Summary section", () => {
    render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText="Partial streaming text"
        isStreaming={true}
      />,
    );

    const summaryText = screen.getByText("Partial streaming text");
    expect(summaryText.closest(".analysis-summary")).toBeInTheDocument();
  });

  it("does not render StreamingAnalysis replacement UI during streaming", () => {
    render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText="streaming"
        isStreaming={true}
      />,
    );

    expect(screen.queryByText("Analyzing your clinical note")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /conditions/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /documentation gaps/i })).not.toBeInTheDocument();
  });

  it("replaces streaming text with analysis.summary after completion", () => {
    const { rerender } = render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText="streaming text"
        isStreaming={true}
      />,
    );

    expect(screen.getByText("streaming text")).toBeInTheDocument();

    rerender(
      <AnalysisReview
        {...defaultProps}
        streamingText={undefined}
        isStreaming={false}
      />,
    );

    expect(screen.getByText("Patient has condition 1.")).toBeInTheDocument();
    expect(screen.queryByText("streaming text")).not.toBeInTheDocument();
  });

  it("shows conditions and gaps only after structured analysis exists", () => {
    render(
      <AnalysisReview
        {...defaultProps}
        analysis={analysis()}
        isStreaming={false}
      />,
    );

    expect(screen.getByRole("heading", { name: /conditions/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /documentation gaps/i })).toBeInTheDocument();
    expect(screen.getByDisplayValue("Condition c1")).toBeInTheDocument();
    expect(screen.getByText("Gap 1")).toBeInTheDocument();
  });

  it("disables review and save actions while streaming", () => {
    render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText="streaming"
        isStreaming={true}
      />,
    );

    expect(screen.queryByRole("button", { name: /save review/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add missed condition/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/reviewer notes/i)).not.toBeInTheDocument();
  });

  it("preserves completed-analysis behavior", () => {
    render(<AnalysisReview {...defaultProps} />);

    expect(screen.getByRole("heading", { name: "Summary" })).toBeInTheDocument();
    expect(screen.getByText("Patient has condition 1.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /conditions/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /documentation gaps/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save review/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/reviewer notes/i)).toBeInTheDocument();
  });

  it("shows 'thinking .' before the first token", () => {
    render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText=""
        isStreaming={true}
      />,
    );

    expect(screen.getByText("thinking .")).toBeInTheDocument();
  });

  it("cycles dot states correctly", () => {
    vi.useFakeTimers();

    render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText=""
        isStreaming={true}
      />,
    );

    const preparing = () =>
      screen.getByText(/thinking|Analyzing note|Preparing summary/);

    expect(preparing()).toBeInTheDocument();
    const before = preparing().textContent;

    act(() => { vi.advanceTimersByTime(500); });

    const after = preparing().textContent;
    expect(after).not.toBe(before);
    expect(preparing()).toBeInTheDocument();

    vi.useRealTimers();
  });

  it("cycles through all messages in order", () => {
    vi.useFakeTimers();

    render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText=""
        isStreaming={true}
      />,
    );

    const messages = ["thinking", "Analyzing note", "Preparing summary"];
    const observed = new Set<string>();
    const capture = () =>
      messages.forEach((m) => {
        if (screen.queryByText(new RegExp(`^${m}`))) observed.add(m);
      });

    capture();
    for (let i = 0; i < 10; i++) {
      act(() => { vi.advanceTimersByTime(1000); });
      capture();
    }

    expect(observed.has("thinking")).toBe(true);
    expect(observed.has("Analyzing note")).toBe(true);
    expect(observed.has("Preparing summary")).toBe(true);

    vi.useRealTimers();
  });

  it("uses spaced dots, not compact ellipsis", () => {
    render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText=""
        isStreaming={true}
      />,
    );

    const paragraph = screen.getByText("thinking .").closest("p");
    expect(paragraph).toHaveTextContent("thinking .");
    expect(paragraph).not.toHaveTextContent("thinking...");
  });

  it("keeps text and dots on one line", () => {
    render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText=""
        isStreaming={true}
      />,
    );

    const paragraph = screen.getByText("thinking .").closest("p");
    expect(paragraph?.tagName).toBe("P");
    expect(paragraph?.innerHTML).not.toMatch(/<br\s*\/?>/);
  });

  it("stops animation and shows real text on first token", () => {
    vi.useFakeTimers();

    const { rerender } = render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText=""
        isStreaming={true}
      />,
    );

    expect(
      screen.getByText(/thinking|Analyzing note|Preparing summary/),
    ).toBeInTheDocument();

    rerender(
      <AnalysisReview
        {...defaultProps}
        streamingText="First token"
        isStreaming={true}
      />,
    );

    expect(screen.getByText("First token")).toBeInTheDocument();
    expect(
      screen.queryByText(/thinking|Analyzing note|Preparing summary/),
    ).not.toBeInTheDocument();

    act(() => { vi.advanceTimersByTime(2000); });
    expect(screen.getByText("First token")).toBeInTheDocument();

    vi.useRealTimers();
  });

  it("cleans up timers after completion", () => {
    vi.useFakeTimers();

    const { rerender } = render(
      <AnalysisReview
        {...defaultProps}
        analysis={null}
        streamingText=""
        isStreaming={true}
      />,
    );

    expect(
      screen.getByText(/thinking|Analyzing note|Preparing summary/),
    ).toBeInTheDocument();

    rerender(
      <AnalysisReview
        {...defaultProps}
        analysis={analysis()}
        streamingText={undefined}
        isStreaming={false}
      />,
    );

    expect(screen.getByText("Patient has condition 1.")).toBeInTheDocument();
    expect(
      screen.queryByText(/thinking|Analyzing note|Preparing summary/),
    ).not.toBeInTheDocument();

    act(() => { vi.advanceTimersByTime(2000); });
    expect(screen.getByText("Patient has condition 1.")).toBeInTheDocument();

    vi.useRealTimers();
  });
});
