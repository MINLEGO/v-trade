import { useState, useMemo } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import {
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import { useUsage } from "@/api/hooks/useUsage";
import { useFiltersStore } from "@/store/filters";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KPICard } from "@/components/KPICard";
import { MicroDollars } from "@/components/MicroDollars";
import { RelativeTime } from "@/components/RelativeTime";
import { AgentFilter } from "@/components/AgentFilter";
import { DollarSign, Cpu, Clock } from "lucide-react";
import type { UsageRow } from "@/api/types";

const LIMIT = 25;

const PIE_COLORS = [
  "#8fd3ff",
  "#34d399",
  "#f59e0b",
  "#f87171",
  "#a78bfa",
  "#fb923c",
  "#38bdf8",
];

const columns: ColumnDef<UsageRow, unknown>[] = [
  { accessorKey: "provider", header: "Provider" },
  { accessorKey: "route", header: "Route" },
  { accessorKey: "usage_kind", header: "Usage Kind" },
  {
    accessorKey: "request_count",
    header: "Requests",
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.request_count.toLocaleString()}</span>
    ),
  },
  {
    accessorKey: "credit_count",
    header: "Credits",
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.credit_count.toLocaleString()}</span>
    ),
  },
  {
    accessorKey: "prompt_tokens",
    header: "Prompt Tokens",
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.prompt_tokens.toLocaleString()}</span>
    ),
  },
  {
    accessorKey: "completion_tokens",
    header: "Completion Tokens",
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.completion_tokens.toLocaleString()}</span>
    ),
  },
  {
    accessorKey: "reasoning_tokens",
    header: "Reasoning Tokens",
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.reasoning_tokens.toLocaleString()}</span>
    ),
  },
  {
    accessorKey: "cached_tokens",
    header: "Cached Tokens",
    cell: ({ row }) => (
      <span className="tabular-nums">{row.original.cached_tokens.toLocaleString()}</span>
    ),
  },
  {
    accessorKey: "billed_cost_micros",
    header: "Billed Cost",
    cell: ({ row }) => <MicroDollars value={row.original.billed_cost_micros} />,
  },
  {
    accessorKey: "nominal_cost_micros",
    header: "Nominal Cost",
    cell: ({ row }) => <MicroDollars value={row.original.nominal_cost_micros} />,
  },
  {
    accessorKey: "average_latency_ms",
    header: "Avg Latency (ms)",
    cell: ({ row }) => (
      <span className="tabular-nums">
        {row.original.average_latency_ms != null
          ? row.original.average_latency_ms.toFixed(1)
          : "—"}
      </span>
    ),
  },
  {
    accessorKey: "last_used_at",
    header: "Last Used",
    cell: ({ row }) => <RelativeTime date={row.original.last_used_at} />,
  },
];

export default function UsagePage() {
  const [offset, setOffset] = useState(0);
  const selectedAgentId = useFiltersStore((s) => s.selectedAgentId);
  const { data, isLoading } = useUsage({
    limit: 500,
    offset: 0,
    agent_id: selectedAgentId ?? undefined,
  });

  const stats = useMemo(() => {
    if (!data || data.length === 0) {
      return { totalCost: 0, totalTokens: 0, avgLatency: 0 };
    }
    const totalCost = data.reduce((s, r) => s + r.billed_cost_micros, 0);
    const totalTokens = data.reduce(
      (s, r) => s + r.prompt_tokens + r.completion_tokens + r.reasoning_tokens,
      0,
    );

    const totalRequests = data.reduce((s, r) => s + r.request_count, 0);
    const avgLatency =
      totalRequests > 0
        ? data.reduce(
            (s, r) =>
              s + (r.average_latency_ms ?? 0) * r.request_count,
            0,
          ) / totalRequests
        : 0;

    return { totalCost, totalTokens, avgLatency };
  }, [data]);

  const costByProvider = useMemo(() => {
    if (!data) return [];
    const map = new Map<string, number>();
    for (const row of data) {
      map.set(
        row.provider,
        (map.get(row.provider) ?? 0) + row.billed_cost_micros,
      );
    }
    return Array.from(map.entries()).map(([name, value]) => ({
      name,
      value: value / 1_000_000,
    }));
  }, [data]);

  const tokenDistribution = useMemo(() => {
    if (!data) return [];
    const map = new Map<
      string,
      { prompt: number; completion: number; reasoning: number }
    >();
    for (const row of data) {
      const existing = map.get(row.provider) ?? {
        prompt: 0,
        completion: 0,
        reasoning: 0,
      };
      existing.prompt += row.prompt_tokens;
      existing.completion += row.completion_tokens;
      existing.reasoning += row.reasoning_tokens;
      map.set(row.provider, existing);
    }
    return Array.from(map.entries()).map(([provider, tokens]) => ({
      provider,
      ...tokens,
    }));
  }, [data]);

  // Client-side pagination for the table
  const paginatedData = (data ?? []).slice(offset, offset + LIMIT);

  return (
    <div>
      <PageHeader
        title="Usage & Cost"
        description="Provider usage and cost breakdown"
        actions={<AgentFilter />}
      />

      <div className="grid gap-4 sm:grid-cols-3 mb-6">
        <KPICard
          title="Total Billed Cost"
          value={`$${(stats.totalCost / 1_000_000).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
          icon={<DollarSign className="size-4" />}
        />
        <KPICard
          title="Total Tokens"
          value={stats.totalTokens.toLocaleString()}
          icon={<Cpu className="size-4" />}
        />
        <KPICard
          title="Avg Latency"
          value={`${stats.avgLatency.toFixed(1)}ms`}
          icon={<Clock className="size-4" />}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2 mb-6">
        <Card className="border-[#1e2a38] bg-[#18202b]">
          <CardHeader>
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Cost by Provider
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie
                  data={costByProvider}
                  cx="50%"
                  cy="50%"
                  innerRadius={60}
                  outerRadius={100}
                  dataKey="value"
                  nameKey="name"
                  label={({ name, value }) =>
                    `${name}: $${value.toFixed(2)}`
                  }
                >
                  {costByProvider.map((_, index) => (
                    <Cell
                      key={`cell-${index}`}
                      fill={PIE_COLORS[index % PIE_COLORS.length]}
                    />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(value) => [`$${Number(value).toFixed(4)}`, "Cost"]}
                  contentStyle={{
                    backgroundColor: "#18202b",
                    border: "1px solid #1e2a38",
                    borderRadius: "6px",
                    color: "#eef3f8",
                  }}
                />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>

        <Card className="border-[#1e2a38] bg-[#18202b]">
          <CardHeader>
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Token Distribution
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={tokenDistribution}>
                <XAxis
                  dataKey="provider"
                  tick={{ fill: "#eef3f8", fontSize: 12 }}
                />
                <YAxis tick={{ fill: "#eef3f8", fontSize: 12 }} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "#18202b",
                    border: "1px solid #1e2a38",
                    borderRadius: "6px",
                    color: "#eef3f8",
                  }}
                />
                <Legend />
                <Bar dataKey="prompt" fill="#8fd3ff" name="Prompt" />
                <Bar dataKey="completion" fill="#34d399" name="Completion" />
                <Bar dataKey="reasoning" fill="#f59e0b" name="Reasoning" />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      </div>

      <DataTable
        data={paginatedData}
        columns={columns}
        isLoading={isLoading}
        pagination={{
          limit: LIMIT,
          offset,
          total: data?.length ?? 0,
          onPageChange: setOffset,
        }}
      />
    </div>
  );
}
