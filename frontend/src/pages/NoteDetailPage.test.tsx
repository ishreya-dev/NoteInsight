import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
import type { Analysis, Note } from "../api/types";

const mockNote: Note = {
  id: "n1",
  user_id: "u1",
  raw_text: "Patient has condition 1.",
  pseudonym: "Patient A",
  visit_date: "2026-01-01",
  created_at: "2026-01-01T00:00:00Z",
  latest_analysis_id: null,
  analysis_job_id: null,
  review_status: "pending",
  condition_count: 0,
};

const mockAnalysis: Analysis = {
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
    vi.mocked(api.reanalyzeNote).mockClear();
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

describe("NoteDetailPage streaming error and retry", () => {
  beforeEach(() => {
    mockNavigate.mockClear();
    vi.mocked(api.getNote).mockClear();
    vi.mocked(api.streamAnalysis).mockClear();
    vi.mocked(api.reanalyzeNote).mockClear();
  });

  it("shows error UI when the stream emits an error event", async () => {
    const failedAnalysis: Analysis = {
      ...mockAnalysis,
      is_failed: true,
      failure_reason: "timeout",
    };

    vi.mocked(api.getNote)
      .mockResolvedValueOnce({
        note: { ...mockNote, analysis_job_id: "job-1" },
        analysis: null,
        review: null,
      })
      .mockResolvedValue({
        note: mockNote,
        analysis: failedAnalysis,
        review: null,
      });

    vi.mocked(api.streamAnalysis).mockImplementation(async (_noteId, handlers) => {
      handlers.onError({ reason: "timeout", message: "Timed out" });
      return undefined;
    });

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(screen.getByText("Analysis timed out")).toBeInTheDocument();
    });

    expect(screen.queryByText(/thinking/)).not.toBeInTheDocument();
  });

  it("retry re-analyzes the note and starts a new stream", async () => {
    const failedAnalysis: Analysis = {
      ...mockAnalysis,
      is_failed: true,
      failure_reason: "timeout",
    };

    vi.mocked(api.getNote)
      .mockResolvedValueOnce({
        note: { ...mockNote, analysis_job_id: "job-1" },
        analysis: null,
        review: null,
      })
      .mockResolvedValue({
        note: mockNote,
        analysis: failedAnalysis,
        review: null,
      });

    vi.mocked(api.streamAnalysis).mockImplementation(async (_noteId, handlers) => {
      handlers.onError({ reason: "timeout", message: "Timed out" });
      return undefined;
    });

    vi.mocked(api.reanalyzeNote).mockResolvedValue({
      note: { ...mockNote, analysis_job_id: "job-2" },
      job_id: "job-2",
    });

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Retry analysis" })).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole("button", { name: "Retry analysis" }));

    expect(vi.mocked(api.reanalyzeNote)).toHaveBeenCalledWith("n1");
    expect(vi.mocked(api.streamAnalysis)).toHaveBeenCalledTimes(2);
  });

  it("reflects retrying state on the retry button", async () => {
    const failedAnalysis: Analysis = {
      ...mockAnalysis,
      is_failed: true,
      failure_reason: "timeout",
    };

    vi.mocked(api.getNote)
      .mockResolvedValueOnce({
        note: { ...mockNote, analysis_job_id: "job-1" },
        analysis: null,
        review: null,
      })
      .mockResolvedValue({
        note: mockNote,
        analysis: failedAnalysis,
        review: null,
      });

    vi.mocked(api.streamAnalysis).mockImplementation(async (_noteId, handlers) => {
      handlers.onError({ reason: "timeout", message: "Timed out" });
      return undefined;
    });

    let resolveReanalyze!: (value: { note: typeof mockNote; job_id: string }) => void;
    const reanalyzePromise = new Promise<{ note: typeof mockNote; job_id: string }>(
      (resolve) => {
        resolveReanalyze = resolve;
      },
    );
    vi.mocked(api.reanalyzeNote).mockImplementation(async () => {
      await reanalyzePromise;
      return { note: { ...mockNote, analysis_job_id: "job-2" }, job_id: "job-2" };
    });

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Retry analysis" })).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole("button", { name: "Retry analysis" }));

    const pendingRetry = () =>
      new Promise<void>((resolve) => setTimeout(resolve, 0));

    await pendingRetry();
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Retry analysis" }),
      ).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(
        screen.getByText(/thinking|Analyzing note|Preparing summary/),
      ).toBeInTheDocument();
    });

    resolveReanalyze!({ note: { ...mockNote, analysis_job_id: "job-2" }, job_id: "job-2" });

    await waitFor(() => {
      expect(vi.mocked(api.streamAnalysis)).toHaveBeenCalledTimes(2);
    });
  });

  it("aborts the previous stream when a new stream starts", async () => {
    const capturedSignals: { first: AbortSignal | null; second: AbortSignal | null } = {
      first: null,
      second: null,
    };

    const failedAnalysis: Analysis = {
      ...mockAnalysis,
      is_failed: true,
      failure_reason: "timeout",
    };

    vi.mocked(api.getNote)
      .mockResolvedValueOnce({
        note: { ...mockNote, analysis_job_id: "job-1" },
        analysis: null,
        review: null,
      })
      .mockResolvedValue({
        note: mockNote,
        analysis: failedAnalysis,
        review: null,
      });

    vi.mocked(api.streamAnalysis).mockImplementation(async (_noteId, handlers) => {
      if (!capturedSignals.first) {
        capturedSignals.first = handlers.signal ?? null;
        handlers.onError({ reason: "timeout", message: "Timed out" });
        return undefined;
      }
      capturedSignals.second = handlers.signal ?? null;
      handlers.onComplete({ note_id: "n1", analysis: mockAnalysis });
      return undefined;
    });

    vi.mocked(api.reanalyzeNote).mockResolvedValue({
      note: { ...mockNote, analysis_job_id: "job-2" },
      job_id: "job-2",
    });

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(api.streamAnalysis).toHaveBeenCalledTimes(1);
    });

    expect(capturedSignals.first).not.toBeNull();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Retry analysis" }),
      ).toBeInTheDocument();
    });

    await userEvent.click(
      screen.getByRole("button", { name: "Retry analysis" }),
    );

    await waitFor(() => {
      expect(api.streamAnalysis).toHaveBeenCalledTimes(2);
    });

    expect(capturedSignals.first?.aborted).toBe(true);
    expect(capturedSignals.second).not.toBeNull();
    expect(capturedSignals.second?.aborted).toBe(false);
  });

  it("does not apply stream completion after unmount", async () => {
    let resolveStream!: () => void;
    const streamPromise = new Promise<void>((resolve) => {
      resolveStream = resolve;
    });

    vi.mocked(api.getNote).mockResolvedValue({
      note: { ...mockNote, analysis_job_id: "job-1" },
      analysis: null,
      review: null,
    });

    vi.mocked(api.streamAnalysis).mockImplementation(async () => {
      await streamPromise;
      return undefined;
    });

    const { unmount } = render(<NoteDetailPage />);

    await waitFor(() => {
      expect(api.streamAnalysis).toHaveBeenCalledTimes(1);
    });

    unmount();

    resolveStream();
    await new Promise((resolve) => setTimeout(resolve, 50));
  });

  it("onError during streaming shows failed analysis state", async () => {
    const failedAnalysis: Analysis = {
      ...mockAnalysis,
      is_failed: true,
      failure_reason: "timeout",
    };

    vi.mocked(api.getNote)
      .mockResolvedValueOnce({
        note: { ...mockNote, analysis_job_id: "job-1" },
        analysis: null,
        review: null,
      })
      .mockResolvedValue({
        note: mockNote,
        analysis: failedAnalysis,
        review: null,
      });

    vi.mocked(api.streamAnalysis).mockImplementation(async (_noteId, handlers) => {
      handlers.onError({ reason: "timeout", message: "Timed out" });
      return undefined;
    });

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(screen.getByText("Analysis timed out")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Retry analysis" }),
      ).toBeInTheDocument();
    });

    expect(
      screen.queryByText(/thinking|Analyzing note|Preparing summary/),
    ).not.toBeInTheDocument();
  });

  it("retry after streaming failure starts analysis again", async () => {
    const failedAnalysis: Analysis = {
      ...mockAnalysis,
      is_failed: true,
      failure_reason: "timeout",
    };

    vi.mocked(api.getNote)
      .mockResolvedValueOnce({
        note: { ...mockNote, analysis_job_id: "job-1" },
        analysis: null,
        review: null,
      })
      .mockResolvedValue({
        note: mockNote,
        analysis: failedAnalysis,
        review: null,
      });

    vi.mocked(api.streamAnalysis).mockImplementation(async (_noteId, handlers) => {
      handlers.onError({ reason: "timeout", message: "Timed out" });
      return undefined;
    });

    let resolveReanalyze!: (value: { note: typeof mockNote; job_id: string }) => void;
    const reanalyzePromise = new Promise<{ note: typeof mockNote; job_id: string }>(
      (resolve) => {
        resolveReanalyze = resolve;
      },
    );
    vi.mocked(api.reanalyzeNote).mockImplementation(async () => {
      await reanalyzePromise;
      return { note: { ...mockNote, analysis_job_id: "job-2" }, job_id: "job-2" };
    });

    render(<NoteDetailPage />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Retry analysis" }),
      ).toBeInTheDocument();
    });

    await userEvent.click(screen.getByRole("button", { name: "Retry analysis" }));

    expect(vi.mocked(api.reanalyzeNote)).toHaveBeenCalledWith("n1");

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Retry analysis" }),
      ).not.toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(
        screen.getByText(/thinking|Analyzing note|Preparing summary/),
      ).toBeInTheDocument();
    });

    resolveReanalyze!({
      note: { ...mockNote, analysis_job_id: "job-2" },
      job_id: "job-2",
    });

    await waitFor(() => {
      expect(vi.mocked(api.streamAnalysis)).toHaveBeenCalledTimes(2);
    });
  });

  it("unmount during streaming aborts the active stream", async () => {
    const capturedSignal: { value: AbortSignal | null } = { value: null };
    let resolveStream!: () => void;
    const streamPromise = new Promise<void>((resolve) => {
      resolveStream = resolve;
    });

    vi.mocked(api.getNote).mockResolvedValue({
      note: { ...mockNote, analysis_job_id: "job-1" },
      analysis: null,
      review: null,
    });

    vi.mocked(api.streamAnalysis).mockImplementation(async (_noteId, handlers) => {
      capturedSignal.value = handlers.signal ?? null;
      await streamPromise;
      return undefined;
    });

    const { unmount } = render(<NoteDetailPage />);

    await waitFor(() => {
      expect(api.streamAnalysis).toHaveBeenCalledTimes(1);
    });

    unmount();

    expect(capturedSignal.value?.aborted).toBe(true);

    resolveStream();
  });
});
