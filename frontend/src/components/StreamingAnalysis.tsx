type StreamingAnalysisProps = {
  text: string;
};

export default function StreamingAnalysis({ text }: StreamingAnalysisProps) {
  return (
    <section className="analysis-streaming" aria-live="polite">
      <h2>Analyzing your clinical note</h2>
      {!text ? (
        <span className="analyzing-ellipsis" aria-label="analyzing">
          <span className="analyzing-dot" />
          <span className="analyzing-dot" />
          <span className="analyzing-dot" />
        </span>
      ) : (
        <pre className="streaming-text">{text}</pre>
      )}
    </section>
  );
}
