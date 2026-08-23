import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const mockNavigate = vi.fn();

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useParams: () => ({ noteId: "n1" }),
  };
});

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    api: {
      ...actual.api,
      getNote: vi.fn(),
      streamAnalysis: vi.fn(),
      reanalyzeNote: vi.fn(),
    },
  };
});

import NoteDetailPage from "./NoteDetailPage";
import { api } from "../api/client";

const mockNote = {
  id: "n1",
  user_id: "u1",
  raw_text: "Patient has condition 1.",
  pseudonym: "Patient A",
  visit_date: "2026-01-01",
  created_at: "2026-01-01T00:00:00Z",
  latest_analysis_id: null,
  analysis_job_id: null,
  review_status: "pending" as const,
  condition_count: 0,
};

const mockAnalysis = {
  id: "a1",
  note_id: "n1",
  user_id: "u1",
  conditions: [],
  gaps: [],
  summary: "Summary text",
  model_version: "test",
  prompt_version: "v1",
  created_at: "2026-01-01T00:00:00Z",
  is_failed: false,
  failure_reason: null,
};

describe("NoteDetailPage header navigation", () => {
  beforeEach(() => {
    mockNavigate.mockClear();
    vi.mocked(api.getNote).mockClear();
    vi.mocked(api.streamAnalysis).mockClear();
  });

  it("renders a New Note button on the Analysis page", async () => {
    vi.mocked(api.getNote).mockResolvedValue({
      note: mockNote,
      analysis: mockAnalysis,
      review: null,
    });

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "New Note" })).toBeInTheDocument();
    });
  });

  it("clicking New Note navigates to /notes/new", async () => {
    vi.mocked(api.getNote).mockResolvedValue({
      note: mockNote,
      analysis: mockAnalysis,
      review: null,
    });

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "New Note" })).toBeInTheDocument();
    });

    await screen.getByRole("button", { name: "New Note" }).click();

    expect(mockNavigate).toHaveBeenCalledWith("/notes/new");
  });

  it("existing History navigation still works", async () => {
    vi.mocked(api.getNote).mockResolvedValue({
      note: mockNote,
      analysis: mockAnalysis,
      review: null,
    });

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Back to history" })).toBeInTheDocument();
    });

    await screen.getByRole("button", { name: "Back to history" }).click();

    expect(mockNavigate).toHaveBeenCalledWith("/history");
  });

  it("shows streamed conditions after the SSE complete event", async () => {
    const streamingNote = { ...mockNote, analysis_job_id: "job-1" };
    const completedAnalysis = {
      ...mockAnalysis,
      id: "a-complete",
      conditions: [
        {
          id: "c1",
          condition_name: "Condition One",
          evidence_quote: "Patient has condition 1.",
          documentation_status: "ambiguous" as const,
          suggested_icd10: "ICD-1",
          confidence: 0.9,
          quote_verified: true,
        },
      ],
    };

    vi.mocked(api.getNote).mockResolvedValue({
      note: streamingNote,
      analysis: null,
      review: null,
    });

    let completeHandler:
      | ((result: { note_id: string; analysis: typeof completedAnalysis }) => void)
      | undefined;
    vi.mocked(api.streamAnalysis).mockImplementation(
      async (_noteId, handlers) => {
        completeHandler = handlers.onComplete;
        return undefined;
      },
    );

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Summary" })).toBeInTheDocument();
    });

    expect(completeHandler).toBeDefined();
    completeHandler!({ note_id: "n1", analysis: completedAnalysis });

    await waitFor(() => {
      expect(screen.getByText(/Conditions \(1\)/)).toBeInTheDocument();
    });

    expect(
      screen.getByDisplayValue("Condition One"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Conditions \(0\)/)).not.toBeInTheDocument();
  });

  it("does not introduce a new note creation flow", async () => {
    vi.mocked(api.getNote).mockResolvedValue({
      note: { ...mockNote, analysis_job_id: "job-1" },
      analysis: null,
      review: null,
    });
    vi.mocked(api.streamAnalysis).mockResolvedValue(undefined);

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "New Note" })).toBeInTheDocument();
    });

    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/clinical note/i)).not.toBeInTheDocument();
  });
});
