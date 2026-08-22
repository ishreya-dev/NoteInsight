import { useEffect, useRef, useState } from "react";
import type { Analysis, DocumentationStatus, Review } from "../api/types";
import { api } from "../api/client";
import { ApiError } from "../api/errors";
import { useReviewState } from "../hooks/useReviewState";
import ConditionCard from "./ConditionCard";
import GapsList from "./GapsList";
import ErrorState from "./ErrorState";

const MESSAGES = ["thinking", "Analyzing note", "Preparing summary"] as const;

interface AnalysisReviewProps {
  analysis: Analysis | null;
  existingReview: Review | null;
  onSaved: (review: Review) => void;
  streamingText?: string;
  isStreaming?: boolean;
}

export default function AnalysisReview({
  analysis,
  existingReview,
  onSaved,
  streamingText,
  isStreaming,
}: AnalysisReviewProps) {
  const streaming = isStreaming ?? (!analysis && !!streamingText);
  const [dotCount, setDotCount] = useState(0);
  const [messageIndex, setMessageIndex] = useState(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isPreparing = streaming && !streamingText;

  useEffect(() => {
    if (!isPreparing) {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      return;
    }

    const prefersReducedMotion =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;

    if (prefersReducedMotion) {
      setDotCount(0);
      setMessageIndex(0);
      return;
    }

    intervalRef.current = setInterval(() => {
      setDotCount((prev) => {
        if (prev === 2) {
          setMessageIndex((mi) => (mi + 1) % MESSAGES.length);
          return 0;
        }
        return prev + 1;
      });
    }, 500);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [isPreparing]);

  const {
    reviewable,
    added,
    updateCondition,
    rejectCondition,
    restoreCondition,
    addCondition,
    updateAddedCondition,
    removeAddedCondition,
    buildPayload,
    hasIncompleteAddedCondition,
  } = useReviewState(analysis?.conditions ?? [], existingReview);

  const [reviewerNotes, setReviewerNotes] = useState(
    existingReview?.reviewer_notes ?? ""
  );
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  const quoteVerifiedById = new Map(
    analysis?.conditions.map((c) => [c.id, c.quote_verified]) ?? []
  );
  const confidenceById = new Map(
    analysis?.conditions.map((c) => [c.id, c.confidence]) ?? []
  );

  async function handleSave() {
    if (!analysis) return;
    setSaving(true);
    setSaveError(null);
    try {
      const review = await api.upsertReview(analysis.id, {
        conditions: buildPayload(),
        gaps: analysis.gaps,
        notes: reviewerNotes.trim() || null,
      });
      setSavedAt(new Date().toLocaleTimeString());
      onSaved(review);
    } catch (err) {
      setSaveError(
        err instanceof ApiError
          ? err.message
          : "Could not save the review. Please try again."
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="analysis-review">
      <section className="analysis-summary">
        <h2>Summary</h2>
        {isPreparing ? (
          <p>
            {MESSAGES[messageIndex]} {".".repeat(dotCount + 1).split("").join(" ")}
          </p>
        ) : streaming ? (
          <p>{streamingText}</p>
        ) : (
          <p>{analysis?.summary}</p>
        )}
      </section>

      {!streaming && (
        <div key={analysis?.id}>
          <section className="analysis-conditions">
        <h2>Conditions ({reviewable.length})</h2>
        {reviewable.map((c) => (
          <ConditionCard
            key={c.sourceConditionId}
            condition={c}
            quoteVerified={quoteVerifiedById.get(c.sourceConditionId) ?? true}
            confidence={confidenceById.get(c.sourceConditionId) ?? 0}
            onChange={(patch) => updateCondition(c.sourceConditionId, patch)}
            onReject={() => rejectCondition(c.sourceConditionId)}
            onRestore={() => restoreCondition(c.sourceConditionId)}
          />
        ))}

        {added.map((c) => (
          <div key={c.localId} className="condition-card added-condition-card">
            <div className="condition-card-header">
              <input
                type="text"
                placeholder="Condition name"
                value={c.conditionName}
                onChange={(e) =>
                  updateAddedCondition(c.localId, { conditionName: e.target.value })
                }
                className="condition-name-input"
              />
              <span className="added-badge">Added by you</span>
            </div>
            <textarea
              placeholder="Evidence quote (must appear in the note)"
              value={c.evidenceQuote}
              onChange={(e) =>
                updateAddedCondition(c.localId, { evidenceQuote: e.target.value })
              }
              rows={2}
            />
            <div className="condition-card-fields">
              <label>
                Documentation status
                <select
                  value={c.documentationStatus}
                  onChange={(e) =>
                    updateAddedCondition(c.localId, {
                      documentationStatus: e.target.value as DocumentationStatus,
                    })
                  }
                >
                  <option value="well_documented">Well documented</option>
                  <option value="ambiguous">Ambiguous</option>
                  <option value="mentioned_without_assessment_or_plan">
                    No assessment or plan
                  </option>
                </select>
              </label>
              <label>
                Suggested ICD-10
                <input
                  type="text"
                  value={c.suggestedIcd10}
                  onChange={(e) =>
                    updateAddedCondition(c.localId, { suggestedIcd10: e.target.value })
                  }
                />
              </label>
            </div>
            <div className="condition-card-actions">
              <button
                type="button"
                onClick={() => removeAddedCondition(c.localId)}
                className="reject-button"
              >
                Remove
              </button>
            </div>
          </div>
        ))}

        <button type="button" onClick={addCondition} className="add-condition-button">
          + Add missed condition
        </button>
      </section>

      <section className="analysis-gaps">
        <h2>Documentation gaps</h2>
        <GapsList gaps={analysis!.gaps} />
      </section>

      <section className="analysis-notes">
        <label htmlFor="reviewer_notes">Reviewer notes (optional)</label>
        <textarea
          id="reviewer_notes"
          rows={3}
          value={reviewerNotes}
          onChange={(e) => setReviewerNotes(e.target.value)}
        />
      </section>

      {saveError && <ErrorState message={saveError} onRetry={handleSave} />}

          <div className="analysis-review-footer">
            {savedAt && !saving && (
              <span className="save-confirmation">Saved at {savedAt}</span>
            )}
            <button 
              type="button" 
              onClick={handleSave} 
              disabled={saving || hasIncompleteAddedCondition || streaming}
            >
              {saving ? "Saving…" : "Save review"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}