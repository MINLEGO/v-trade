import { useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { useOperatorActions } from "@/api/hooks/useOperatorActions";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { Badge } from "@/components/ui/badge";
import { RelativeTime } from "@/components/RelativeTime";
import { JsonExpand } from "@/components/JsonExpand";
import type { OperatorActionRow } from "@/api/types";

const LIMIT = 25;

const actionBadgeClass: Record<string, string> = {
  pause: "bg-yellow-600/20 text-yellow-400 border-yellow-600/30",
  resume: "bg-green-600/20 text-green-400 border-green-600/30",
};

const columns: ColumnDef<OperatorActionRow, unknown>[] = [
  {
    accessorKey: "occurred_at",
    header: "Time",
    cell: ({ row }) => <RelativeTime date={row.original.occurred_at} />,
  },
  {
    accessorKey: "actor_id",
    header: "Actor",
    cell: ({ row }) => (
      <span className="font-mono text-xs">{row.original.actor_id}</span>
    ),
  },
  {
    accessorKey: "action",
    header: "Action",
    cell: ({ row }) => (
      <Badge
        variant="outline"
        className={actionBadgeClass[row.original.action] ?? "bg-gray-600/20 text-gray-400 border-gray-600/30"}
      >
        {row.original.action}
      </Badge>
    ),
  },
  { accessorKey: "target_type", header: "Target Type" },
  {
    accessorKey: "target_id",
    header: "Target ID",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.target_id
          ? `${row.original.target_id.slice(0, 8)}…`
          : "System"}
      </span>
    ),
  },
  {
    accessorKey: "before_state",
    header: "Before State",
    cell: ({ row }) => <JsonExpand data={row.original.before_state} label="Before" />,
  },
  {
    accessorKey: "after_state",
    header: "After State",
    cell: ({ row }) => <JsonExpand data={row.original.after_state} label="After" />,
  },
  {
    accessorKey: "idempotency_key",
    header: "Idempotency Key",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.idempotency_key.slice(0, 8)}…
      </span>
    ),
  },
];

export default function AuditLogPage() {
  const [offset, setOffset] = useState(0);
  const { data, isLoading } = useOperatorActions({
    limit: LIMIT,
    offset,
  });

  return (
    <div>
      <PageHeader
        title="Audit Log"
        description="Operator action history"
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
