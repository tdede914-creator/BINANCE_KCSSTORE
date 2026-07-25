import clsx from "clsx";

interface StatCardProps {
  label: string;
  value: string | number;
  hint?: string;
  className?: string;
}

export function StatCard({ label, value, hint, className }: StatCardProps) {
  return (
    <div
      className={clsx(
        "bg-bg-card border border-border rounded-lg p-4",
        className,
      )}
    >
      <div className="text-xs text-muted uppercase tracking-wider">
        {label}
      </div>
      <div className="text-2xl font-semibold font-mono mt-1">{value}</div>
      {hint && <div className="text-xs text-muted mt-1">{hint}</div>}
    </div>
  );
}
