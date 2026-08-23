import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { ApiError } from "../api/errors";
import { useAuth } from "../auth/useAuth";
import type { NoteCreatePayload } from "../api/types";
import NoteForm from "../components/NoteForm";
import ErrorState from "../components/ErrorState";

export default function NewNotePage() {
  const { signOut } = useAuth();
  const navigate = useNavigate();

  const [status, setStatus] =
    useState<"idle" | "loading" | "success" | "error">("idle");

  const [error, setError] =
    useState<string | null>(null);

  async function handleSubmit(
    payload: NoteCreatePayload,
  ) {
    setStatus("loading");
    setError(null);

    try {
      const res = await api.createNote(payload);
      navigate(`/notes/${res.note.id}`);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not create this note. Please try again.",
      );

      setStatus("error");
    }
  }

  function startNewNote() {
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

      {status !== "success" && (
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

      {status === "success" && (
        <button
          type="button"
          onClick={startNewNote}
        >
          Start a different note
        </button>
      )}
    </div>
  );
}
