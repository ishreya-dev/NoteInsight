import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { ApiError } from "../api/errors";
import { useAuth } from "../auth/useAuth";
import type {
  Analysis,
  AsyncStatus,
  Note,
  NoteCreatePayload,
} from "../api/types";
import NoteForm from "../components/NoteForm";
import AnalysisReview from "../components/AnalysisReview";
import FailedAnalysisNotice from "../components/FailedAnalysisNotice";
import ErrorState from "../components/ErrorState";
import AnalysisProgress from "../components/AnalysisProgress";

type AnalysisResult = {
  note: Note;
  analysis: Analysis | null;
};

export default function NewNotePage() {
  const { signOut } = useAuth();
  const navigate = useNavigate();

  const [status, setStatus] =
    useState<AsyncStatus>("idle");

  const [error, setError] =
    useState<string | null>(null);

  const [result, setResult] =
    useState<AnalysisResult | null>(null);
  const [stage, setStage] = useState("preparing");
  const streamAbort = useRef<AbortController | null>(null);

  useEffect(() => () => streamAbort.current?.abort(), []);

  async function stream(note: Note) {
    streamAbort.current?.abort();
    const controller = new AbortController();
    streamAbort.current = controller;
    try {
      await api.streamAnalysis(note.id, {
        signal: controller.signal,
        onStatus: (event) => setStage(event.stage),
        onComplete: (event) => {
          setResult({ note, analysis: event.analysis });
          setStatus("success");
        },
        onError: async (message) => {
          setError(message);
          const detail = await api.getNote(note.id).catch(() => null);
          setResult(detail ? { note: detail.note, analysis: detail.analysis } : { note, analysis: null });
          setStatus("error");
        },
      });
    } catch (err) {
      if (!controller.signal.aborted) {
        setError(err instanceof ApiError ? err.message : "Could not analyze this note.");
        setStatus("error");
      }
    }
  }

  async function handleSubmit(
    payload: NoteCreatePayload,
  ) {
    setStatus("loading");
    setError(null);

    try {
      const res = await api.createNote(payload);
      setResult({ note: res.note, analysis: null });
      setStage("preparing");
      void stream(res.note);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not analyze this note. Please try again.",
      );

      setStatus("error");
    }
  }

  async function handleRetryAnalysis() {
    if (!result) return;

    setStatus("loading");
    setError(null);

    try {
      const res = await api.reanalyzeNote(result.note.id);
      setResult({ note: res.note, analysis: null });
      setStage("preparing");
      void stream(res.note);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Retry failed. Please try again.",
      );

      setStatus("error");
    }
  }

  function startNewNote() {
    streamAbort.current?.abort();
    setResult(null);
    setError(null);
    setStatus("idle");
  }

  const isLoading = status === "loading";

  return (
    <div className="page">
      <header className="page-header">
        <h1>Note Insight</h1>

        <nav>
          <button
            type="button"
            onClick={() => navigate("/history")}
          >
            History
          </button>

          <button
            type="button"
            onClick={() => void signOut()}
          >
            Sign out
          </button>
        </nav>
      </header>

      {!result && (
        <>
          <NoteForm
            onSubmit={handleSubmit}
            submitting={isLoading}
          />

          {error && (
            <ErrorState message={error} />
          )}
        </>
      )}

      {result && isLoading && <AnalysisProgress stage={stage} />}

      {result?.analysis?.is_failed && (
        <>
          <FailedAnalysisNotice
            analysis={result.analysis}
            onRetry={handleRetryAnalysis}
            retrying={isLoading}
          />

          {error && (
            <ErrorState message={error} />
          )}

          <button
            type="button"
            onClick={startNewNote}
            disabled={isLoading}
          >
            Start a different note
          </button>
        </>
      )}

      {result?.analysis && !result.analysis.is_failed && (
        <>
          <AnalysisReview
            analysis={result.analysis}
            existingReview={null}
            onSaved={() =>
              navigate(`/notes/${result.note.id}`)
            }
          />

          <button
            type="button"
            onClick={startNewNote}
          >
            Start a different note
          </button>
        </>
      )}
    </div>
  );
}