import { useState, useMemo } from "react";
import { Link } from "react-router-dom";
import type { ColumnDef } from "@tanstack/react-table";
import { useCycles } from "@/api/hooks/useCycles";
import { useFiltersStore } from "@/store/filters";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { KPICard } from "@/components/KPICard";
import { StatusBadge } from "@/components/StatusBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { JsonExpand } from "@/components/JsonExpand";
import { AgentFilter } from "@/components/AgentFilter";
import { Activity, CheckCircle2, Loader2, XCircle, SkipForward } from "lucide-react";
import type { CycleRow } from "@/api/types";

const LIMIT = 25;

const columns: ColumnDef<CycleRow, unknown>[] = [
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
    accessorKey: "id",
    header: "Cycle ID",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.id.slice(0, 8)}…
      </span>
    ),
  },
  {
    accessorKey: "scheduled_at",
    header: "Scheduled At",
    cell: ({ row }) => <RelativeTime date={row.original.scheduled_at} />,
  },
  {
    accessorKey: "data_cutoff",
    header: "Data Cutoff",
    cell: ({ row }) => <RelativeTime date={row.original.data_cutoff} />,
  },
  {
    accessorKey: "status",
    header: "Status",
    cell: ({ row }) => <StatusBadge status={row.original.status} />,
  },
  {
    accessorKey: "started_at",
    header: "Started At",
    cell: ({ row }) =>
      row.original.started_at ? (
        <RelativeTime date={row.original.started_at} />
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
  {
    accessorKey: "completed_at",
    header: "Completed At",
    cell: ({ row }) =>
      row.original.completed_at ? (
        <RelativeTime date={row.original.completed_at} />
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
  {
    accessorKey: "model_termination_status",
    header: "Model Termination",
    cell: ({ row }) => (
      <span>{row.original.model_termination_status ?? "—"}</span>
    ),
  },
  {
    accessorKey: "failure_reason",
    header: "Failure Reason",
    cell: ({ row }) =>
      row.original.failure_reason ? (
        <JsonExpand data={row.original.failure_reason} label="Show reason" />
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
  },
  {
    accessorKey: "prompt_version",
    header: "Prompt Version",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.prompt_version ?? "—"}
      </span>
    ),
  },
  {
    accessorKey: "code_version",
    header: "Code Version",
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.code_version ?? "—"}
      </span>
    ),
  },
];

export default function CyclesPage() {
  const [offset, setOffset] = useState(0);
  const selectedAgentId = useFiltersStore((s) => s.selectedAgentId);
  const { data, isLoading } = useCycles({
    limit: LIMIT,
    offset,
    agent_id: selectedAgentId ?? undefined,
  });

  const stats = useMemo(() => {
    if (!data) return { total: 0, completed: 0, running: 0, failed: 0, skipped: 0 };
    return {
      total: data.length,
      completed: data.filter((c) => c.status === "completed").length,
      running: data.filter((c) => c.status === "running").length,
      failed: data.filter((c) => c.status === "failed").length,
      skipped: data.filter((c) => c.status === "skipped").length,
    };
  }, [data]);

  return (
    <div>
      <PageHeader
        title="Cycles"
        description="Agent decision cycles"
        actions={<AgentFilter />}
      />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5 mb-6">
        <KPICard
          title="Total Cycles"
          value={stats.total}
          icon={<Activity className="size-4" />}
        />
        <KPICard
          title="Completed"
          value={stats.completed}
          icon={<CheckCircle2 className="size-4" />}
        />
        <KPICard
          title="Running"
          value={stats.running}
          icon={<Loader2 className="size-4" />}
        />
        <KPICard
          title="Failed"
          value={stats.failed}
          icon={<XCircle className="size-4" />}
        />
        <KPICard
          title="Skipped"
          value={stats.skipped}
          icon={<SkipForward className="size-4" />}
        />
      </div>

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
