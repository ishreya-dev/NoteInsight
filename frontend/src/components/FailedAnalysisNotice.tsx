import { getFailureDisplay } from "../api/failure";
import type { Analysis } from "../api/types";

interface FailedAnalysisNoticeProps {
  analysis: Analysis;
  onRetry: () => void;
  retrying: boolean;
}

export default function FailedAnalysisNotice({
  analysis,
  onRetry,
  retrying,
}: FailedAnalysisNoticeProps) {
  const display = getFailureDisplay(analysis.failure_reason);

  return (
    <div role="alert" className="failed-analysis-notice">
      <h3>{display.title}</h3>
      <p>{display.message}</p>
      <button type="button" onClick={onRetry} disabled={retrying}>
        {retrying ? "Retrying…" : "Retry analysis"}
      </button>
    </div>
  );
}