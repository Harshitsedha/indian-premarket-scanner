type Props = { label: string; value: number };

export function SignalBar({ label, value }: Props) {
  const pct = Math.min(Math.max(value, 0), 1) * 100;
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <span style={{ color: "var(--muted)" }} className="text-xs truncate">
        {label}
      </span>
      <div style={{ backgroundColor: "var(--border)" }} className="h-1 rounded-full">
        <div
          style={{ backgroundColor: "var(--accent)", width: `${pct}%` }}
          className="h-1 rounded-full transition-all"
        />
      </div>
      <span style={{ color: "var(--muted)" }} className="text-xs font-mono">
        {value.toFixed(2)}
      </span>
    </div>
  );
}
