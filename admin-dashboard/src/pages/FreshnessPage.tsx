import type { ColumnDef } from "@tanstack/react-table";
import { useFreshness } from "@/api/hooks/useFreshness";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { RelativeTime } from "@/components/RelativeTime";
import { Database, Newspaper, Globe, TrendingUp } from "lucide-react";
import type { FreshnessRow } from "@/api/types";

const sourceIcons: Record<string, React.ReactNode> = {
  polymarket: <Globe className="size-4" />,
  internal: <Database className="size-4" />,
  external: <Newspaper className="size-4" />,
};

function formatAge(seconds: number | null): { text: string; color: string } {
  if (seconds === null) return { text: "Never", color: "text-red-400" };
  if (seconds < 300) return { text: `${Math.floor(seconds / 60)}m ${seconds % 60}s`, color: "text-green-400" };
  if (seconds < 3600) return { text: `${Math.floor(seconds / 60)}m ${seconds % 60}s`, color: "text-yellow-400" };
  return { text: `${Math.floor(seconds / 60)}m ${seconds % 60}s`, color: "text-red-400" };
}

const columns: ColumnDef<FreshnessRow, unknown>[] = [
  {
    accessorKey: "source",
    header: "Source",
    cell: ({ row }) => {
      const source = row.original.source.toLowerCase();
      return (
        <div className="flex items-center gap-2">
          {sourceIcons[source] ?? <TrendingUp className="size-4" />}
          <span className="capitalize">{row.original.source}</span>
        </div>
      );
    },
  },
  {
    accessorKey: "last_observed_at",
    header: "Last Observed At",
    cell: ({ row }) =>
      row.original.last_observed_at ? (
        <RelativeTime date={row.original.last_observed_at} />
      ) : (
        <span className="text-muted-foreground">Never</span>
      ),
  },
  {
    accessorKey: "age_seconds",
    header: "Age",
    cell: ({ row }) => {
      const { text, color } = formatAge(row.original.age_seconds);
      return <span className={color}>{text}</span>;
    },
  },
  {
    accessorKey: "record_count",
    header: "Record Count",
    cell: ({ row }) => (
      <span className="tabular-nums">
        {row.original.record_count.toLocaleString()}
      </span>
    ),
  },
];

export default function FreshnessPage() {
  const { data, isLoading } = useFreshness();

  return (
    <div>
      <PageHeader
        title="Data Freshness"
        description="Source data freshness monitoring"
      />

      <DataTable
        data={data ?? []}
        columns={columns}
        isLoading={isLoading}
      />
    </div>
  );
}
