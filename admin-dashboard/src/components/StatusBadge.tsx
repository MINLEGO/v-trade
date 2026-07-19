import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type StatusVariant = {
  label: string;
  className: string;
};

const statusMap: Record<string, StatusVariant> = {
  running: { label: "Running", className: "bg-green-600/20 text-green-400 border-green-600/30" },
  completed: { label: "Completed", className: "bg-green-600/20 text-green-400 border-green-600/30" },
  failed: { label: "Failed", className: "bg-red-600/20 text-red-400 border-red-600/30" },
  paused: { label: "Paused", className: "bg-yellow-600/20 text-yellow-400 border-yellow-600/30" },
  skipped: { label: "Skipped", className: "bg-gray-600/20 text-gray-400 border-gray-600/30" },
  open: { label: "Open", className: "bg-yellow-600/20 text-yellow-400 border-yellow-600/30" },
  resolved: { label: "Resolved", className: "bg-green-600/20 text-green-400 border-green-600/30" },
  fresh: { label: "Fresh", className: "bg-green-600/20 text-green-400 border-green-600/30" },
  stale: { label: "Stale", className: "bg-yellow-600/20 text-yellow-400 border-yellow-600/30" },
  missing: { label: "Missing", className: "bg-red-600/20 text-red-400 border-red-600/30" },
  ready: { label: "Ready", className: "bg-green-600/20 text-green-400 border-green-600/30" },
  not_ready: { label: "Not Ready", className: "bg-red-600/20 text-red-400 border-red-600/30" },
};

interface StatusBadgeProps {
  status: string;
}

export function StatusBadge({ status }: StatusBadgeProps) {
  const config = statusMap[status] ?? {
    label: status,
    className: "bg-gray-600/20 text-gray-400 border-gray-600/30",
  };

  return (
    <Badge
      variant="outline"
      className={cn("text-xs", config.className)}
    >
      {config.label}
    </Badge>
  );
}
