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
  return (
    <div role="alert" className="failed-analysis-notice">
      <h3>Analysis failed</h3>
      <p>
        The model could not produce a valid analysis for this note.
        {analysis.failure_reason && (
          <>
            {" "}
            <span className="failed-analysis-reason">
              {analysis.failure_reason}
            </span>
          </>
        )}
      </p>
      <button type="button" onClick={onRetry} disabled={retrying}>
        {retrying ? "Retrying…" : "Retry analysis"}
      </button>
    </div>
  );
}