import { useState } from "react";
import { Link } from "react-router-dom";
import type { ColumnDef } from "@tanstack/react-table";
import { useLeaderboard } from "@/api/hooks/useLeaderboard";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { MicroDollars } from "@/components/MicroDollars";
import { RelativeTime } from "@/components/RelativeTime";
import { StatusBadge } from "@/components/StatusBadge";
import { PauseButton } from "@/components/PauseButton";
import type { LeaderboardRow } from "@/api/types";

const LIMIT = 25;

const columns: ColumnDef<LeaderboardRow, unknown>[] = [
  {
    accessorKey: "agent_name",
    header: "Agent",
    cell: ({ row }) => (
      <Link
        to={`/agents/${row.original.agent_id}`}
        className="text-[#8fd3ff] hover:underline"
      >
        {row.original.agent_name}
      </Link>
    ),
  },
  {
    accessorKey: "model_label",
    header: "Model",
    cell: ({ row }) => (
      <span className="text-muted-foreground">
        {row.original.model_label}
      </span>
    ),
  },
  {
    accessorKey: "account_value_micros",
    header: "Account Value",
    cell: ({ row }) => (
      <MicroDollars value={row.original.account_value_micros} />
    ),
  },
  {
    accessorKey: "realized_pnl_micros",
    header: "Realized PnL",
    cell: ({ row }) => (
      <MicroDollars value={row.original.realized_pnl_micros} showSign />
    ),
  },
  {
    accessorKey: "unrealized_pnl_micros",
    header: "Unrealized PnL",
    cell: ({ row }) => (
      <MicroDollars value={row.original.unrealized_pnl_micros} showSign />
    ),
  },
  {
    accessorKey: "total_pnl_micros",
    header: "Total PnL",
    cell: ({ row }) => (
      <MicroDollars value={row.original.total_pnl_micros} showSign />
    ),
  },
  {
    accessorKey: "drawdown_fraction",
    header: "Drawdown",
    cell: ({ row }) => {
      const value = row.original.drawdown_fraction;
      if (value == null) return <span className="text-muted-foreground">—</span>;
      const pct = value * 100;
      return (
        <div className="flex items-center gap-2">
          <div className="h-1.5 w-16 overflow-hidden rounded-full bg-[#1e2a38]">
            <div
              className="h-full rounded-full bg-red-500"
              style={{ width: `${Math.min(pct, 100)}%` }}
            />
          </div>
          <span className="tabular-nums text-sm text-red-400">
            {pct.toFixed(1)}%
          </span>
        </div>
      );
    },
  },
  {
    accessorKey: "calculated_at",
    header: "Last Updated",
    cell: ({ row }) =>
      row.original.calculated_at ? (
        <RelativeTime date={row.original.calculated_at} />
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
  {
    id: "status",
    header: "Status",
    cell: ({ row }) => (
      <StatusBadge
        status={row.original.paused_at ? "paused" : "running"}
      />
    ),
  },
  {
    id: "actions",
    header: "",
    cell: ({ row }) => (
      <PauseButton
        agentId={row.original.agent_id}
        isPaused={!!row.original.paused_at}
      />
    ),
  },
];

export default function LeaderboardPage() {
  const [offset, setOffset] = useState(0);
  const { data, isLoading } = useLeaderboard({ limit: LIMIT, offset });

  return (
    <div>
      <PageHeader title="Leaderboard" />

      <DataTable
        data={data ?? []}
        columns={columns}
        isLoading={isLoading}
        pagination={{
          limit: LIMIT,
          offset,
          total: data ? (data.length < LIMIT ? offset + data.length : undefined) : undefined,
          onPageChange: setOffset,
        }}
      />
    </div>
  );
}