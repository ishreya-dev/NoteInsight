import type { AnalysisFailureReason } from "./types";

export interface FailureDisplay {
  title: string;
  message: string;
}

const failureDisplays: Record<AnalysisFailureReason, FailureDisplay> = {
  rate_limited: {
    title: "Analysis temporarily unavailable",
    message: "The AI service is currently receiving too many requests. Please wait a moment and try again.",
  },
  invalid_output: {
    title: "Analysis failed",
    message: "The model could not produce a valid analysis for this note. Please try again.",
  },
  timeout: {
    title: "Analysis timed out",
    message: "The AI service took too long to respond. Please try again.",
  },
  provider_error: {
    title: "Analysis unavailable",
    message: "We couldn't complete the analysis right now. Please try again.",
  },
  unknown: {
    title: "Analysis failed",
    message: "Something went wrong while analyzing this note. Please try again.",
  },
};

export function getFailureDisplay(reason: string | null): FailureDisplay {
  return failureDisplays[reason as AnalysisFailureReason] ?? failureDisplays.unknown;
}