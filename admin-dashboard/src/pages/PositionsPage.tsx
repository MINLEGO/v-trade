import { useState } from "react";
import { Link } from "react-router-dom";
import type { ColumnDef } from "@tanstack/react-table";
import { usePositions } from "@/api/hooks/usePositions";
import { useFiltersStore } from "@/store/filters";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { MicroDollars } from "@/components/MicroDollars";
import { RelativeTime } from "@/components/RelativeTime";
import { StatusBadge } from "@/components/StatusBadge";
import { AgentFilter } from "@/components/AgentFilter";
import type { PositionRow } from "@/api/types";

const LIMIT = 25;

const columns: ColumnDef<PositionRow, unknown>[] = [
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
    accessorKey: "question",
    header: "Market",
    cell: ({ row }) => (
      <span className="max-w-[200px] truncate" title={row.original.question}>
        {row.original.question}
      </span>
    ),
  },
  { accessorKey: "outcome", header: "Outcome" },
  {
    accessorKey: "shares",
    header: "Shares",
    cell: ({ row }) => <span className="tabular-nums">{row.original.shares}</span>,
  },
  {
    accessorKey: "average_cost",
    header: "Average Cost",
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.average_cost.toFixed(4)}</span>
    ),
  },
  {
    accessorKey: "cost_basis_micros",
    header: "Cost Basis",
    cell: ({ row }) => <MicroDollars value={row.original.cost_basis_micros} />,
  },
  {
    accessorKey: "realized_pnl_micros",
    header: "Realized PnL",
    cell: ({ row }) => (
      <MicroDollars value={row.original.realized_pnl_micros} showSign />
    ),
  },
  {
    accessorKey: "best_bid",
    header: "Best Bid",
    cell: ({ row }) => (
      <span className="tabular-nums">
        {row.original.best_bid != null
          ? row.original.best_bid.toFixed(4)
          : "—"}
      </span>
    ),
  },
  {
    accessorKey: "liquidation_value_micros",
    header: "Liquidation Value",
    cell: ({ row }) => (
      <MicroDollars value={row.original.liquidation_value_micros} />
    ),
  },
  {
    accessorKey: "valuation_status",
    header: "Valuation Status",
    cell: ({ row }) => <StatusBadge status={row.original.valuation_status} />,
  },
  {
    accessorKey: "quote_age_seconds",
    header: "Quote Age",
    cell: ({ row }) => {
      const age = row.original.quote_age_seconds;
      if (age == null)
        return <span className="text-muted-foreground">—</span>;
      return (
        <span className={age > 300 ? "text-red-400" : ""}>{age}s</span>
      );
    },
  },
  {
    accessorKey: "updated_at",
    header: "Updated",
    cell: ({ row }) => <RelativeTime date={row.original.updated_at} />,
  },
];

export default function PositionsPage() {
  const [offset, setOffset] = useState(0);
  const selectedAgentId = useFiltersStore((s) => s.selectedAgentId);
  const { data, isLoading } = usePositions({
    limit: LIMIT,
    offset,
    agent_id: selectedAgentId ?? undefined,
  });

  return (
    <div>
      <PageHeader
        title="Positions"
        description="All active positions across agents"
        actions={<AgentFilter />}
      />

      <DataTable
        data={data ?? []}
        columns={columns}
        isLoading={isLoading}
        pagination={{
          limit: LIMIT,
          offset,
          onPageChange: setOffset,
        }}
      />
    </div>
  );
}