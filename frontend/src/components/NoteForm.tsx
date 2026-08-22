import { useState, type FormEvent } from "react";
import type { NoteCreatePayload } from "../api/types";

interface NoteFormProps {
  onSubmit: (payload: NoteCreatePayload) => void;
  submitting: boolean;
}

const MAX_RAW_TEXT_WORDS = 6_000;

export default function NoteForm({
  onSubmit,
  submitting,
}: NoteFormProps) {
  const [rawText, setRawText] = useState("");
  const [pseudonym, setPseudonym] = useState("");
  const [visitDate, setVisitDate] = useState("");
  const [validationError, setValidationError] =
    useState<string | null>(null);

  const trimmedText = rawText.trim();

  const wordCount = trimmedText
    ? trimmedText.split(/\s+/).length
    : 0;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();

    if (!trimmedText) {
      setValidationError("Please paste the clinical note text.");
      return;
    }

    if (trimmedText.length > 20_000) {
      setValidationError(
        `Note is too long (${trimmedText.length} characters, max 20,000).`,
      );
      return;
    }

    if (wordCount > MAX_RAW_TEXT_WORDS) {
      setValidationError(
        `Note is too long (${wordCount} words, max ${MAX_RAW_TEXT_WORDS}).`,
      );
      return;
    }

    setValidationError(null);

    onSubmit({
      raw_text: trimmedText,
      pseudonym: pseudonym.trim() || null,
      visit_date: visitDate || null,
    });
  }

  return (
    <form className="note-form" onSubmit={handleSubmit}>
      <label htmlFor="raw_text">Clinical note</label>

      <textarea
        id="raw_text"
        rows={14}
        value={rawText}
        onChange={(e) => {
          setRawText(e.target.value);

          if (validationError) {
            setValidationError(null);
          }
        }}
        placeholder="Paste the free-text clinical note here…"
        disabled={submitting}
        required
      />

      <span className="note-form-hint">
        {wordCount} word{wordCount === 1 ? "" : "s"}
      </span>

      <div className="note-form-row">
        <div>
          <label htmlFor="pseudonym">
            Patient pseudonym (optional)
          </label>

          <input
            id="pseudonym"
            type="text"
            value={pseudonym}
            onChange={(e) => setPseudonym(e.target.value)}
            placeholder="e.g. Patient A"
            disabled={submitting}
            maxLength={100}
          />
        </div>

        <div>
          <label htmlFor="visit_date">
            Visit date (optional)
          </label>

          <input
            id="visit_date"
            type="date"
            value={visitDate}
            onChange={(e) => setVisitDate(e.target.value)}
            disabled={submitting}
          />
        </div>
      </div>

      {validationError && (
        <p role="alert" className="note-form-error">
          {validationError}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting || wordCount < 1}
      >
        {submitting ? "Analyzing…" : "Analyze note"}
      </button>
    </form>
  );
}