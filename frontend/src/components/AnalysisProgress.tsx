const stages = [
  ["preparing", "Preparing clinical analysis"],
  ["analyzing_conditions", "Analyzing conditions and documentation gaps"],
  ["finalizing", "Finalizing analysis"],
] as const;

export default function AnalysisProgress({ stage }: { stage: string }) {
  const activeIndex = stages.findIndex(([key]) => key === stage);
  return (
    <section className="analysis-progress" aria-live="polite">
      <h2>Analyzing your clinical note</h2>
      <ol>
        {stages.map(([key, label], index) => (
          <li
            key={key}
            className={index < activeIndex ? "complete" : index === activeIndex ? "active" : ""}
          >
            <span aria-hidden="true">{index < activeIndex ? "✓" : index === activeIndex ? "●" : "○"}</span>
            {label}
          </li>
        ))}
      </ol>
    </section>
  );
}