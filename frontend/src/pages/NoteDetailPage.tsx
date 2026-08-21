import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { ApiError } from "../api/errors";
import type { Analysis, Note, Review } from "../api/types";
import AnalysisReview from "../components/AnalysisReview";
import FailedAnalysisNotice from "../components/FailedAnalysisNotice";
import LoadingState from "../components/LoadingState";
import ErrorState from "../components/ErrorState";
import AnalysisProgress from "../components/AnalysisProgress";

export default function NoteDetailPage() {
  const { noteId } = useParams<{ noteId: string }>();
  const navigate = useNavigate();

  const [note, setNote] =
    useState<Note | null>(null);

  const [analysis, setAnalysis] =
    useState<Analysis | null>(null);

  const [review, setReview] =
    useState<Review | null>(null);

  const [loading, setLoading] = useState(true);

  const [error, setError] =
    useState<string | null>(null);

  const [retrying, setRetrying] =
    useState(false);
  const [stage, setStage] = useState("preparing");
  const streamAbort = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (!noteId) {
      setError("Note not found.");
      setLoading(false);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const res = await api.getNote(noteId);

      setNote(res.note);
      setAnalysis(res.analysis);
      setReview(res.review);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not load this note.",
      );
    } finally {
      setLoading(false);
    }
  }, [noteId]);

  useEffect(() => {
    void load();
    return () => streamAbort.current?.abort();
  }, [load]);

  async function handleRetryAnalysis() {
    if (!noteId || retrying) return;

    setRetrying(true);
    setError(null);

    try {
      const res = await api.reanalyzeNote(noteId);

      setNote(res.note);
      setAnalysis(null);
      setReview(null);
      setStage("preparing");

      const controller = new AbortController();
      streamAbort.current = controller;
      await api.streamAnalysis(noteId, {
        signal: controller.signal,
        onStatus: (event) => setStage(event.stage),
        onComplete: (event) => setAnalysis(event.analysis),
        onError: (message) => setError(message),
      });
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Retry failed.",
      );
    } finally {
      setRetrying(false);
    }
  }

  if (loading) {
    return <LoadingState label="Loading note…" />;
  }

  if (error) {
    return (
      <ErrorState
        message={error}
        onRetry={load}
      />
    );
  }

  if (!note) {
    return (
      <ErrorState message="Note not found." />
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>{note.pseudonym ?? "Note"}</h1>

        <nav>
          <button
            type="button"
            onClick={() => navigate("/history")}
          >
            Back to history
          </button>
        </nav>
      </header>

      <details className="note-raw-text">
        <summary>View original note text</summary>

        <pre>{note.raw_text}</pre>
      </details>

      {!analysis && (
        retrying ? (
          <AnalysisProgress stage={stage} />
        ) : (
          <ErrorState
            message="This note has no analysis yet."
            onRetry={handleRetryAnalysis}
          />
        )
      )}

      {analysis?.is_failed && (
        <FailedAnalysisNotice
          analysis={analysis}
          onRetry={handleRetryAnalysis}
          retrying={retrying}
        />
      )}

      {analysis && !analysis.is_failed && (
        <AnalysisReview
          analysis={analysis}
          existingReview={review}
          onSaved={setReview}
        />
      )}
    </div>
  );
}