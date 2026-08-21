import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { ApiError } from "../api/errors";
import type { NoteListItem } from "../api/types";
import HistoryList from "../components/HistoryList";
import LoadingState from "../components/LoadingState";
import ErrorState from "../components/ErrorState";

export default function HistoryPage() {
  const navigate = useNavigate();

  const [items, setItems] = useState<NoteListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const res = await api.listNotes();
      setItems(res.items);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not load your note history.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="page">
      <header className="page-header">
        <h1>History</h1>

        <nav>
          <button
            type="button"
            onClick={() => navigate("/notes/new")}
          >
            New note
          </button>
        </nav>
      </header>

      {loading && <LoadingState label="Loading history…" />}

      {!loading && error && (
        <ErrorState
          message={error}
          onRetry={load}
        />
      )}

      {!loading && !error && (
        <HistoryList items={items} />
      )}
    </div>
  );
}