export default function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" className="loading-state">
      {label}
    </div>
  );
}