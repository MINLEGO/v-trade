import { cn } from "@/lib/utils";

interface MicroDollarsProps {
  value: number | null;
  showSign?: boolean;
  className?: string;
}

const MICRO = 1_000_000;

export function MicroDollars({ value, showSign, className }: MicroDollarsProps) {
  if (value === null || value === undefined) {
    return <span className={cn("text-muted-foreground", className)}>—</span>;
  }

  const dollars = value / MICRO;
  const formatted = dollars.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  const sign = showSign && value > 0 ? "+" : "";
  const color =
    value > 0 ? "text-green-400" : value < 0 ? "text-red-400" : "";

  return (
    <span className={cn("tabular-nums", color, className)}>
      {sign}
      {formatted}
    </span>
  );
}
