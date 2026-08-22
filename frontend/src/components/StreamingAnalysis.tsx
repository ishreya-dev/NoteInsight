type StreamingAnalysisProps = {
  text: string;
};

export default function StreamingAnalysis({ text }: StreamingAnalysisProps) {
  return (
    <section className="analysis-streaming" aria-live="polite">
      <h2>Analyzing your clinical note</h2>
      <pre className="streaming-text">{text}</pre>
    </section>
  );
}
