import { useState, useCallback, useMemo, useRef } from "react";
import type {
  Condition,
  ConditionReviewInput,
  ConditionReviewStatus,
  Review,
} from "../api/types";

interface ReviewableCondition {
  sourceConditionId: string;
  conditionName: string;
  evidenceQuote: string;
  documentationStatus: Condition["documentation_status"];
  suggestedIcd10: string;
  status: Extract<
    ConditionReviewStatus,
    "accepted" | "edited" | "rejected"
  >;
}

interface AddedCondition {
  localId: string;
  conditionName: string;
  evidenceQuote: string;
  documentationStatus: Condition["documentation_status"];
  suggestedIcd10: string;
}

function seedReviewable(
  initialConditions: Condition[],
  existingReview: Review | null,
): ReviewableCondition[] {
  if (!existingReview) {
    return initialConditions.map((condition) => ({
      sourceConditionId: condition.id,
      conditionName: condition.condition_name,
      evidenceQuote: condition.evidence_quote,
      documentationStatus: condition.documentation_status,
      suggestedIcd10: condition.suggested_icd10,
      status: "accepted" as const,
    }));
  }

  return existingReview.conditions
    .filter((c) => c.source_condition_id !== null && c.status !== "added")
    .map((c) => ({
      sourceConditionId: c.source_condition_id as string,
      conditionName: c.condition_name,
      evidenceQuote: c.evidence_quote,
      documentationStatus: c.documentation_status,
      suggestedIcd10: c.suggested_icd10,
      status: c.status as ReviewableCondition["status"],
    }));
}

function seedAdded(existingReview: Review | null): AddedCondition[] {
  if (!existingReview) return [];

  return existingReview.conditions
    .filter((c) => c.status === "added")
    .map((c, i) => ({
      localId: `added-existing-${i}`,
      conditionName: c.condition_name,
      evidenceQuote: c.evidence_quote,
      documentationStatus: c.documentation_status,
      suggestedIcd10: c.suggested_icd10,
    }));
}

export function useReviewState(
  initialConditions: Condition[],
  existingReview: Review | null = null,
) {
  const addedIdCounter = useRef(0);

  const [reviewable, setReviewable] = useState<ReviewableCondition[]>(() =>
    seedReviewable(initialConditions, existingReview),
  );

  const [added, setAdded] = useState<AddedCondition[]>(() =>
    seedAdded(existingReview),
  );

  const updateCondition = useCallback(
    (
      sourceConditionId: string,
      patch: Partial<ReviewableCondition>,
    ) => {
      setReviewable((previous) =>
        previous.map((condition) =>
          condition.sourceConditionId === sourceConditionId
            ? {
                ...condition,
                ...patch,
                status:
                  patch.status ??
                  (condition.status === "rejected"
                    ? "rejected"
                    : "edited"),
              }
            : condition,
        ),
      );
    },
    [],
  );

  const rejectCondition = useCallback(
    (sourceConditionId: string) => {
      setReviewable((previous) =>
        previous.map((condition) =>
          condition.sourceConditionId === sourceConditionId
            ? { ...condition, status: "rejected" as const }
            : condition,
        ),
      );
    },
    [],
  );

  const restoreCondition = useCallback(
    (sourceConditionId: string) => {
      setReviewable((previous) =>
        previous.map((condition) =>
          condition.sourceConditionId === sourceConditionId
            ? { ...condition, status: "accepted" as const }
            : condition,
        ),
      );
    },
    [],
  );

  const addCondition = useCallback(() => {
    addedIdCounter.current += 1;

    setAdded((previous) => [
      ...previous,
      {
        localId: `added-${addedIdCounter.current}`,
        conditionName: "",
        evidenceQuote: "",
        documentationStatus: "ambiguous",
        suggestedIcd10: "",
      },
    ]);
  }, []);

  const updateAddedCondition = useCallback(
    (
      localId: string,
      patch: Partial<AddedCondition>,
    ) => {
      setAdded((previous) =>
        previous.map((condition) =>
          condition.localId === localId
            ? { ...condition, ...patch }
            : condition,
        ),
      );
    },
    [],
  );

  const removeAddedCondition = useCallback((localId: string) => {
    setAdded((previous) =>
      previous.filter(
        (condition) => condition.localId !== localId,
      ),
    );
  }, []);

  const buildPayload = useCallback((): ConditionReviewInput[] => {
    const fromReviewable: ConditionReviewInput[] =
      reviewable.map((condition) => ({
        source_condition_id: condition.sourceConditionId,
        condition_name: condition.conditionName,
        evidence_quote: condition.evidenceQuote,
        documentation_status:
          condition.documentationStatus,
        suggested_icd10: condition.suggestedIcd10,
        status: condition.status,
      }));

    const fromAdded: ConditionReviewInput[] = added
      .filter(
        (condition) =>
          condition.conditionName.trim() &&
          condition.evidenceQuote.trim(),
      )
      .map((condition) => ({
        source_condition_id: null,
        condition_name: condition.conditionName,
        evidence_quote: condition.evidenceQuote,
        documentation_status:
          condition.documentationStatus,
        suggested_icd10: condition.suggestedIcd10,
        status: "added" as const,
      }));

    return [...fromReviewable, ...fromAdded];
  }, [reviewable, added]);

  const hasIncompleteAddedCondition = useMemo(
    () =>
      added.some(
        (condition) =>
          !condition.conditionName.trim() ||
          !condition.evidenceQuote.trim(),
      ),
    [added],
  );

  return {
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
  };
}

export type {
  ReviewableCondition,
  AddedCondition,
};
