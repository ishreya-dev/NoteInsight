import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { getFailureDisplay } from "../api/failure";
import { ApiError } from "../api/errors";
import type { Analysis, Note, Review } from "../api/types";
import AnalysisReview from "../components/AnalysisReview";
import FailedAnalysisNotice from "../components/FailedAnalysisNotice";
import LoadingState from "../components/LoadingState";
import ErrorState from "../components/ErrorState";

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
  const [streamingText, setStreamingText] = useState("");
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

  async function startStream(targetNoteId: string) {
    streamAbort.current?.abort();
    const controller = new AbortController();
    streamAbort.current = controller;
    setStreamingText("");
    try {
      await api.streamAnalysis(targetNoteId, {
        signal: controller.signal,
        onStatus: () => {},
        onToken: (text: string) => {
          setStreamingText((prev) => prev + text);
        },
        onComplete: (event) => {
          setAnalysis(event.analysis);
          setStreamingText("");
        },
        onError: async ({ reason }) => {
          const detail = await api.getNote(targetNoteId).catch(() => null);
          if (detail) {
            setNote(detail.note);
            setAnalysis(detail.analysis);
            setReview(detail.review);
            setError(null);
          } else {
            setError(getFailureDisplay(reason).message);
          }
          setStreamingText("");
        },
      });
    } catch (err) {
      if (!controller.signal.aborted) {
        setError(err instanceof ApiError ? err.message : "Could not analyze this note.");
        setStreamingText("");
      }
    }
  }

  useEffect(() => {
    void load();
    return () => streamAbort.current?.abort();
  }, [load]);

  useEffect(() => {
    if (note && !analysis && !loading && !retrying && !error) {
      if (note.analysis_job_id) {
        void startStream(note.id);
      }
    }
  }, [note, analysis, loading, retrying, error]);

  async function handleRetryAnalysis() {
    if (!noteId || retrying) return;

    setRetrying(true);
    setError(null);
    setAnalysis(null);
    setReview(null);

    try {
      const res = await api.reanalyzeNote(noteId);
      setNote(res.note);
      await startStream(noteId);
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

  const isStreaming = !analysis && (retrying || streamingText.length > 0 || !!note.analysis_job_id);

  return (
    <div className="page">
      <header className="page-header">
        <h1>{note.pseudonym ?? "Note"}</h1>

        <nav>
          <button
            type="button"
            onClick={() => navigate("/notes/new")}
          >
            New Note
          </button>

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

      {(analysis || isStreaming) && !analysis?.is_failed && (
        <AnalysisReview
          key={analysis?.id ?? undefined}
          analysis={analysis ?? null}
          existingReview={review}
          onSaved={setReview}
          streamingText={isStreaming ? streamingText : undefined}
          isStreaming={isStreaming}
        />
      )}

      {analysis?.is_failed && (
        <FailedAnalysisNotice
          analysis={analysis}
          onRetry={handleRetryAnalysis}
          retrying={retrying}
        />
      )}
    </div>
  );
}
