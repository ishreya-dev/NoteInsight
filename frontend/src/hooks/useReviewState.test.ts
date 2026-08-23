import { renderHook, act } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useReviewState } from "./useReviewState";
import type { Condition } from "../api/types";

const condition: Condition = {
  id: "c1",
  condition_name: "Type 2 diabetes",
  evidence_quote: "patient has diabetes",
  documentation_status: "ambiguous",
  suggested_icd10: "E11.9",
  confidence: 0.8,
  quote_verified: true,
};

describe("useReviewState", () => {
  it("seeds reviewable conditions as accepted", () => {
    const { result } = renderHook(() => useReviewState([condition]));

    expect(result.current.reviewable).toHaveLength(1);
    expect(result.current.reviewable[0].status).toBe("accepted");
  });

  it("marks a condition as edited when a field changes", () => {
    const { result } = renderHook(() => useReviewState([condition]));

    act(() => {
      result.current.updateCondition("c1", { conditionName: "Diabetes, type 2" });
    });

    expect(result.current.reviewable[0].status).toBe("edited");
    expect(result.current.reviewable[0].conditionName).toBe("Diabetes, type 2");
  });

  it("rejects and restores a condition", () => {
    const { result } = renderHook(() => useReviewState([condition]));

    act(() => {
      result.current.rejectCondition("c1");
    });
    expect(result.current.reviewable[0].status).toBe("rejected");

    act(() => {
      result.current.restoreCondition("c1");
    });
    expect(result.current.reviewable[0].status).toBe("accepted");
  });

  it("flags an incomplete added condition and excludes it from the payload", () => {
    const { result } = renderHook(() => useReviewState([condition]));

    act(() => {
      result.current.addCondition();
    });

    expect(result.current.hasIncompleteAddedCondition).toBe(true);
    expect(result.current.buildPayload()).toHaveLength(1);
  });

  it("includes a completed added condition in the payload", () => {
    const { result } = renderHook(() => useReviewState([condition]));

    act(() => {
      result.current.addCondition();
    });

    const localId = result.current.added[0].localId;

    act(() => {
      result.current.updateAddedCondition(localId, {
        conditionName: "Hypertension",
        evidenceQuote: "blood pressure elevated",
      });
    });

    expect(result.current.hasIncompleteAddedCondition).toBe(false);

    const payload = result.current.buildPayload();
    expect(payload).toHaveLength(2);
    expect(payload[1]).toMatchObject({
      source_condition_id: null,
      condition_name: "Hypertension",
      status: "added",
    });
  });
});