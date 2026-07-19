import { useState } from "react";
import { Link } from "react-router-dom";
import type { ColumnDef } from "@tanstack/react-table";
import { useSettlements } from "@/api/hooks/useSettlements";
import { useFiltersStore } from "@/store/filters";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { MicroDollars } from "@/components/MicroDollars";
import { RelativeTime } from "@/components/RelativeTime";
import { StatusBadge } from "@/components/StatusBadge";
import { AgentFilter } from "@/components/AgentFilter";
import type { SettlementRow } from "@/api/types";

const LIMIT = 25;

const columns: ColumnDef<SettlementRow, unknown>[] = [
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
    accessorKey: "settled_at",
    header: "Time",
    cell: ({ row }) => <RelativeTime date={row.original.settled_at} />,
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
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.shares}</span>
    ),
  },
  {
    accessorKey: "payout_micros",
    header: "Payout",
    cell: ({ row }) => <MicroDollars value={row.original.payout_micros} />,
  },
  {
    accessorKey: "realized_pnl_micros",
    header: "Realized PnL",
    cell: ({ row }) => (
      <MicroDollars value={row.original.realized_pnl_micros} showSign />
    ),
  },
  {
    accessorKey: "result",
    header: "Result",
    cell: ({ row }) => <StatusBadge status={row.original.result} />,
  },
  {
    accessorKey: "source_created_at",
    header: "Source Created",
    cell: ({ row }) => <RelativeTime date={row.original.source_created_at} />,
  },
  {
    accessorKey: "as_of_cutoff",
    header: "As-of Cutoff",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.as_of_cutoff.slice(0, 16)}…
      </span>
    ),
  },
];

export default function SettlementsPage() {
  const [offset, setOffset] = useState(0);
  const selectedAgentId = useFiltersStore((s) => s.selectedAgentId);
  const { data, isLoading } = useSettlements({
    limit: LIMIT,
    offset,
    agent_id: selectedAgentId ?? undefined,
  });

  return (
    <div>
      <PageHeader
        title="Settlements"
        description="All settlement events across agents"
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
