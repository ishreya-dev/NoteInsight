import type { DocumentationStatus } from "../api/types";
import type { ReviewableCondition } from "../hooks/useReviewState";

const STATUS_LABELS: Record<
  DocumentationStatus,
  string
> = {
  well_documented: "Well documented",
  ambiguous: "Ambiguous",
  mentioned_without_assessment_or_plan:
    "No assessment or plan",
};

interface ConditionCardProps {
  condition: ReviewableCondition;
  quoteVerified: boolean;
  confidence: number;
  onChange: (
    patch: Partial<ReviewableCondition>,
  ) => void;
  onReject: () => void;
  onRestore: () => void;
  disabled?: boolean;
}

export default function ConditionCard({
  condition,
  quoteVerified,
  confidence,
  onChange,
  onReject,
  onRestore,
  disabled = false,
}: ConditionCardProps) {
  const isRejected = condition.status === "rejected";

  const isFieldDisabled =
    isRejected || disabled;

  return (
    <div
      className={
        `condition-card${
          isRejected
            ? " condition-card-rejected"
            : ""
        }`
      }
    >
      <div className="condition-card-header">
        <input
          type="text"
          value={condition.conditionName}
          onChange={(e) =>
            onChange({
              conditionName: e.target.value,
            })
          }
          disabled={isFieldDisabled}
          className="condition-name-input"
        />

        {!quoteVerified && (
          <span
            className="quote-unverified-badge"
            title={
              "This quote could not be matched " +
              "verbatim in the original note"
            }
          >
            Unverified quote
          </span>
        )}

        <span className="confidence-badge">
          {Math.round(confidence * 100)}%
          {" confidence"}
        </span>
      </div>

      <blockquote className="evidence-quote">
        &ldquo;{condition.evidenceQuote}&rdquo;
      </blockquote>

      <div className="condition-card-fields">
        <label>
          Documentation status

          <select
            value={condition.documentationStatus}
            onChange={(e) =>
              onChange({
                documentationStatus:
                  e.target.value as DocumentationStatus,
              })
            }
            disabled={isFieldDisabled}
          >
            {Object.entries(STATUS_LABELS).map(
              ([value, label]) => (
                <option
                  key={value}
                  value={value}
                >
                  {label}
                </option>
              ),
            )}
          </select>
        </label>

        <label>
          Suggested ICD-10

          <input
            type="text"
            value={condition.suggestedIcd10}
            onChange={(e) =>
              onChange({
                suggestedIcd10: e.target.value,
              })
            }
            disabled={isFieldDisabled}
          />
        </label>
      </div>

      <div className="condition-card-actions">
        {isRejected ? (
          <button
            type="button"
            onClick={onRestore}
            disabled={disabled}
          >
            Restore
          </button>
        ) : (
          <button
            type="button"
            onClick={onReject}
            className="reject-button"
            disabled={disabled}
          >
            Reject
          </button>
        )}
      </div>
    </div>
  );
}