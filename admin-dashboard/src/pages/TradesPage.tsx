import { useState } from "react";
import { Link } from "react-router-dom";
import type { ColumnDef } from "@tanstack/react-table";
import { useTrades } from "@/api/hooks/useTrades";
import { useFiltersStore } from "@/store/filters";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { Badge } from "@/components/ui/badge";
import { MicroDollars } from "@/components/MicroDollars";
import { RelativeTime } from "@/components/RelativeTime";
import { AgentFilter } from "@/components/AgentFilter";
import type { TradeRow } from "@/api/types";

const LIMIT = 25;

const columns: ColumnDef<TradeRow, unknown>[] = [
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
    accessorKey: "filled_at",
    header: "Time",
    cell: ({ row }) => <RelativeTime date={row.original.filled_at} />,
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
    accessorKey: "side",
    header: "Side",
    cell: ({ row }) => (
      <Badge
        variant="outline"
        className={
          row.original.side === "BUY"
            ? "bg-green-600/20 text-green-400 border-green-600/30"
            : "bg-red-600/20 text-red-400 border-red-600/30"
        }
      >
        {row.original.side}
      </Badge>
    ),
  },
  {
    accessorKey: "shares",
    header: "Shares",
    cell: ({ row }) => <span className="tabular-nums">{row.original.shares}</span>,
  },
  {
    accessorKey: "price",
    header: "Price",
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.price.toFixed(4)}</span>
    ),
  },
  {
    accessorKey: "gross_micros",
    header: "Gross",
    cell: ({ row }) => <MicroDollars value={row.original.gross_micros} />,
  },
  {
    accessorKey: "fee_micros",
    header: "Fee",
    cell: ({ row }) => <MicroDollars value={row.original.fee_micros} />,
  },
  { accessorKey: "policy", header: "Policy" },
  {
    accessorKey: "agent_cycle_id",
    header: "Cycle ID",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.agent_cycle_id.slice(0, 8)}…
      </span>
    ),
  },
];

export default function TradesPage() {
  const [offset, setOffset] = useState(0);
  const selectedAgentId = useFiltersStore((s) => s.selectedAgentId);
  const { data, isLoading } = useTrades({
    limit: LIMIT,
    offset,
    agent_id: selectedAgentId ?? undefined,
  });

  return (
    <div>
      <PageHeader
        title="Trades"
        description="All trade fills across agents"
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