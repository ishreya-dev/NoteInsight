import type { DocumentationGap } from "../api/types";

export default function GapsList({ gaps }: { gaps: DocumentationGap[] }) {
  if (gaps.length === 0) {
    return <p className="gaps-empty">No documentation gaps identified.</p>;
  }

  return (
    <ul className="gaps-list">
      {gaps.map((gap, i) => (
        <li key={i}>
          {gap.description}
          {gap.related_condition && (
            <span className="gap-related-condition">
              {" "}
              — related to {gap.related_condition}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}