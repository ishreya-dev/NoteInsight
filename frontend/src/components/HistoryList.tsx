import { Link } from "react-router-dom";
import type { NoteListItem } from "../api/types";

export default function HistoryList({ items }: { items: NoteListItem[] }) {
  if (items.length === 0) {
    return <p>No notes yet. Submit your first note to get started.</p>;
  }

  return (
    <ul className="history-list">
      {items.map((item) => (
        <li key={item.id}>
          <Link to={`/notes/${item.id}`} className="history-item">
            <span className="history-item-date">
              {new Date(item.created_at).toLocaleDateString()}
            </span>
            <span className="history-item-pseudonym">
              {item.pseudonym ?? "—"}
            </span>
            <span className="history-item-count">
              {item.condition_count} condition
              {item.condition_count === 1 ? "" : "s"}
            </span>
            <span className={`history-item-status status-${item.review_status}`}>
              {item.review_status === "reviewed" ? "Reviewed" : "Pending"}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}