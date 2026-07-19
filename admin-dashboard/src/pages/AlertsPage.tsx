import { useState, useMemo } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { useAlerts } from "@/api/hooks/useAlerts";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/StatusBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { JsonExpand } from "@/components/JsonExpand";
import { Link } from "react-router-dom";
import type { AlertRow } from "@/api/types";

const LIMIT = 25;

type FilterMode = "all" | "open" | "resolved";

const columns: ColumnDef<AlertRow, unknown>[] = [
  {
    accessorKey: "severity",
    header: "Severity",
    cell: ({ row }) => <StatusBadge status={row.original.severity} />,
  },
  {
    accessorKey: "code",
    header: "Code",
    cell: ({ row }) => (
      <span className="font-mono text-xs">{row.original.code}</span>
    ),
  },
  {
    accessorKey: "details",
    header: "Details",
    cell: ({ row }) => <JsonExpand data={row.original.details} label="Show details" />,
  },
  {
    accessorKey: "run_id",
    header: "Run ID",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.run_id.slice(0, 8)}…
      </span>
    ),
  },
  {
    accessorKey: "agent_id",
    header: "Agent ID",
    cell: ({ row }) => (
      <Link
        to={`/agents/${row.original.agent_id}`}
        className="font-mono text-xs text-[#8fd3ff] hover:underline"
      >
        {row.original.agent_id.slice(0, 8)}…
      </Link>
    ),
  },
  {
    accessorKey: "opened_at",
    header: "Opened At",
    cell: ({ row }) => <RelativeTime date={row.original.opened_at} />,
  },
  {
    accessorKey: "acknowledged_at",
    header: "Acknowledged At",
    cell: ({ row }) =>
      row.original.acknowledged_at ? (
        <RelativeTime date={row.original.acknowledged_at} />
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
  {
    accessorKey: "resolved_at",
    header: "Resolved At",
    cell: ({ row }) =>
      row.original.resolved_at ? (
        <RelativeTime date={row.original.resolved_at} />
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
];

export default function AlertsPage() {
  const [offset, setOffset] = useState(0);
  const [filterMode, setFilterMode] = useState<FilterMode>("all");
  const { data, isLoading } = useAlerts({ limit: 500, offset: 0 });

  const filteredData = useMemo(() => {
    if (!data) return [];
    if (filterMode === "open") return data.filter((a) => a.resolved_at === null);
    if (filterMode === "resolved") return data.filter((a) => a.resolved_at !== null);
    return data;
  }, [data, filterMode]);

  // Client-side pagination
  const paginatedData = filteredData.slice(offset, offset + LIMIT);

  const filterButtons: { label: string; mode: FilterMode }[] = [
    { label: "All", mode: "all" },
    { label: "Open", mode: "open" },
    { label: "Resolved", mode: "resolved" },
  ];

  return (
    <div>
      <PageHeader
        title="Alerts"
        description="System alerts and notifications"
        actions={
          <div className="flex items-center gap-1 rounded-md border border-[#1e2a38] p-0.5">
            {filterButtons.map((btn) => (
              <Button
                key={btn.mode}
                variant={filterMode === btn.mode ? "default" : "ghost"}
                size="sm"
                className={
                  filterMode === btn.mode
                    ? "bg-[#8fd3ff]/20 text-[#8fd3ff] hover:bg-[#8fd3ff]/30"
                    : "text-muted-foreground"
                }
                onClick={() => {
                  setFilterMode(btn.mode);
                  setOffset(0);
                }}
              >
                {btn.label}
              </Button>
            ))}
          </div>
        }
      />

      <DataTable
        data={paginatedData}
        columns={columns}
        isLoading={isLoading}
        pagination={{
          limit: LIMIT,
          offset,
          total: filteredData.length,
          onPageChange: setOffset,
        }}
      />
    </div>
  );
}
