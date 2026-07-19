import { useState } from "react";
import { Link } from "react-router-dom";
import type { ColumnDef } from "@tanstack/react-table";
import { useRejections } from "@/api/hooks/useRejections";
import { useFiltersStore } from "@/store/filters";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { Badge } from "@/components/ui/badge";
import { RelativeTime } from "@/components/RelativeTime";
import { AgentFilter } from "@/components/AgentFilter";
import type { RejectionRow } from "@/api/types";

const LIMIT = 25;

const columns: ColumnDef<RejectionRow, unknown>[] = [
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
    accessorKey: "created_at",
    header: "Time",
    cell: ({ row }) => <RelativeTime date={row.original.created_at} />,
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
  { accessorKey: "validation_status", header: "Validation Status" },
  {
    accessorKey: "rejection_code",
    header: "Rejection Code",
    cell: ({ row }) => (
      <Badge
        variant="outline"
        className="bg-red-600/20 text-red-400 border-red-600/30"
      >
        {row.original.rejection_code}
      </Badge>
    ),
  },
  {
    accessorKey: "order_status",
    header: "Order Status",
    cell: ({ row }) => (
      <span>{row.original.order_status ?? "—"}</span>
    ),
  },
  {
    accessorKey: "agent_cycle_id",
    header: "Cycle ID",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.agent_cycle_id.slice(0, 8)}…
      </span>
    ),
  },
  {
    accessorKey: "intent_id",
    header: "Intent ID",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.intent_id.slice(0, 8)}…
      </span>
    ),
  },
];

export default function RejectionsPage() {
  const [offset, setOffset] = useState(0);
  const selectedAgentId = useFiltersStore((s) => s.selectedAgentId);
  const { data, isLoading } = useRejections({
    limit: LIMIT,
    offset,
    agent_id: selectedAgentId ?? undefined,
  });

  return (
    <div>
      <PageHeader
        title="Rejections"
        description="Rejected order intents"
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
