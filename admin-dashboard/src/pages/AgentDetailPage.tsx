import { useState } from "react";
import { useParams } from "react-router-dom";
import type { ColumnDef } from "@tanstack/react-table";
import { useLeaderboard } from "@/api/hooks/useLeaderboard";
import { usePositions } from "@/api/hooks/usePositions";
import { useTrades } from "@/api/hooks/useTrades";
import { useSettlements } from "@/api/hooks/useSettlements";
import { useRejections } from "@/api/hooks/useRejections";
import { useCycles } from "@/api/hooks/useCycles";
import { useUsage } from "@/api/hooks/useUsage";
import { PageHeader } from "@/components/layout/PageHeader";
import { DataTable } from "@/components/DataTable";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { MicroDollars } from "@/components/MicroDollars";
import { RelativeTime } from "@/components/RelativeTime";
import { StatusBadge } from "@/components/StatusBadge";
import { JsonExpand } from "@/components/JsonExpand";
import { PauseButton } from "@/components/PauseButton";
import type {
  PositionRow,
  TradeRow,
  SettlementRow,
  RejectionRow,
  CycleRow,
  UsageRow,
} from "@/api/types";

const LIMIT = 25;

// ─── Positions columns ────────────────────────────────────────────
const positionColumns: ColumnDef<PositionRow, unknown>[] = [
  { accessorKey: "question", header: "Market" },
  { accessorKey: "outcome", header: "Outcome" },
  { accessorKey: "shares", header: "Shares", cell: ({ row }) => <span className="tabular-nums">{row.original.shares}</span> },
  { accessorKey: "average_cost", header: "Avg Cost", cell: ({ row }) => <span className="tabular-nums">{row.original.average_cost.toFixed(4)}</span> },
  { accessorKey: "cost_basis_micros", header: "Cost Basis", cell: ({ row }) => <MicroDollars value={row.original.cost_basis_micros} /> },
  { accessorKey: "realized_pnl_micros", header: "Realized PnL", cell: ({ row }) => <MicroDollars value={row.original.realized_pnl_micros} showSign /> },
  { accessorKey: "best_bid", header: "Best Bid", cell: ({ row }) => <span className="tabular-nums">{row.original.best_bid != null ? row.original.best_bid.toFixed(4) : "—"}</span> },
  { accessorKey: "liquidation_value_micros", header: "Liquidation Value", cell: ({ row }) => <MicroDollars value={row.original.liquidation_value_micros} /> },
  { accessorKey: "valuation_status", header: "Valuation Status", cell: ({ row }) => <StatusBadge status={row.original.valuation_status} /> },
  { accessorKey: "quote_age_seconds", header: "Quote Age", cell: ({ row }) => {
    const age = row.original.quote_age_seconds;
    if (age == null) return <span className="text-muted-foreground">—</span>;
    return <span className={age > 300 ? "text-red-400" : ""}>{age}s</span>;
  }},
  { accessorKey: "updated_at", header: "Updated", cell: ({ row }) => <RelativeTime date={row.original.updated_at} /> },
];

// ─── Trades columns ───────────────────────────────────────────────
const tradeColumns: ColumnDef<TradeRow, unknown>[] = [
  { accessorKey: "fill_id", header: "Fill ID", cell: ({ row }) => <span className="font-mono text-xs">{row.original.fill_id.slice(0, 8)}…</span> },
  { accessorKey: "filled_at", header: "Time", cell: ({ row }) => <RelativeTime date={row.original.filled_at} /> },
  { accessorKey: "question", header: "Market" },
  { accessorKey: "outcome", header: "Outcome" },
  { accessorKey: "side", header: "Side", cell: ({ row }) => (
    <Badge variant="outline" className={row.original.side === "BUY" ? "bg-green-600/20 text-green-400 border-green-600/30" : "bg-red-600/20 text-red-400 border-red-600/30"}>
      {row.original.side}
    </Badge>
  )},
  { accessorKey: "shares", header: "Shares", cell: ({ row }) => <span className="tabular-nums">{row.original.shares}</span> },
  { accessorKey: "price", header: "Price", cell: ({ row }) => <span className="tabular-nums">{row.original.price.toFixed(4)}</span> },
  { accessorKey: "gross_micros", header: "Gross", cell: ({ row }) => <MicroDollars value={row.original.gross_micros} /> },
  { accessorKey: "fee_micros", header: "Fee", cell: ({ row }) => <MicroDollars value={row.original.fee_micros} /> },
  { accessorKey: "policy", header: "Policy" },
];

// ─── Settlements columns ──────────────────────────────────────────
const settlementColumns: ColumnDef<SettlementRow, unknown>[] = [
  { accessorKey: "settled_at", header: "Time", cell: ({ row }) => <RelativeTime date={row.original.settled_at} /> },
  { accessorKey: "question", header: "Market" },
  { accessorKey: "outcome", header: "Outcome" },
  { accessorKey: "shares", header: "Shares", cell: ({ row }) => <span className="tabular-nums">{row.original.shares}</span> },
  { accessorKey: "payout_micros", header: "Payout", cell: ({ row }) => <MicroDollars value={row.original.payout_micros} /> },
  { accessorKey: "realized_pnl_micros", header: "PnL", cell: ({ row }) => <MicroDollars value={row.original.realized_pnl_micros} showSign /> },
  { accessorKey: "result", header: "Result", cell: ({ row }) => <StatusBadge status={row.original.result} /> },
  { accessorKey: "as_of_cutoff", header: "As-of Cutoff", cell: ({ row }) => <span className="text-xs text-muted-foreground">{new Date(row.original.as_of_cutoff).toLocaleString()}</span> },
];

// ─── Rejections columns ───────────────────────────────────────────
const rejectionColumns: ColumnDef<RejectionRow, unknown>[] = [
  { accessorKey: "intent_id", header: "Intent ID", cell: ({ row }) => <span className="font-mono text-xs">{row.original.intent_id.slice(0, 8)}…</span> },
  { accessorKey: "created_at", header: "Time", cell: ({ row }) => <RelativeTime date={row.original.created_at} /> },
  { accessorKey: "question", header: "Market" },
  { accessorKey: "outcome", header: "Outcome" },
  { accessorKey: "side", header: "Side", cell: ({ row }) => (
    <Badge variant="outline" className={row.original.side === "BUY" ? "bg-green-600/20 text-green-400 border-green-600/30" : "bg-red-600/20 text-red-400 border-red-600/30"}>
      {row.original.side}
    </Badge>
  )},
  { accessorKey: "validation_status", header: "Validation Status" },
  { accessorKey: "rejection_code", header: "Rejection Code", cell: ({ row }) => (
    <Badge variant="outline" className="bg-red-600/20 text-red-400 border-red-600/30">
      {row.original.rejection_code}
    </Badge>
  )},
  { accessorKey: "order_status", header: "Order Status", cell: ({ row }) => row.original.order_status ?? <span className="text-muted-foreground">—</span> },
  { accessorKey: "agent_cycle_id", header: "Cycle ID", cell: ({ row }) => <span className="font-mono text-xs">{row.original.agent_cycle_id.slice(0, 8)}…</span> },
];

// ─── Cycles columns ───────────────────────────────────────────────
const cycleColumns: ColumnDef<CycleRow, unknown>[] = [
  { accessorKey: "id", header: "Cycle ID", cell: ({ row }) => <span className="font-mono text-xs">{row.original.id.slice(0, 8)}…</span> },
  { accessorKey: "scheduled_at", header: "Scheduled", cell: ({ row }) => <RelativeTime date={row.original.scheduled_at} /> },
  { accessorKey: "data_cutoff", header: "Data Cutoff", cell: ({ row }) => <span className="text-xs text-muted-foreground">{new Date(row.original.data_cutoff).toLocaleString()}</span> },
  { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
  { accessorKey: "started_at", header: "Started", cell: ({ row }) => row.original.started_at ? <RelativeTime date={row.original.started_at} /> : <span className="text-muted-foreground">—</span> },
  { accessorKey: "completed_at", header: "Completed", cell: ({ row }) => row.original.completed_at ? <RelativeTime date={row.original.completed_at} /> : <span className="text-muted-foreground">—</span> },
  { accessorKey: "model_termination_status", header: "Termination", cell: ({ row }) => row.original.model_termination_status ?? <span className="text-muted-foreground">—</span> },
  { accessorKey: "failure_reason", header: "Failure Reason", cell: ({ row }) => {
    const reason = row.original.failure_reason;
    if (!reason) return <span className="text-muted-foreground">—</span>;
    try {
      return <JsonExpand data={JSON.parse(reason)} label="Show reason" />;
    } catch {
      return <span className="text-xs">{reason}</span>;
    }
  }},
  { accessorKey: "prompt_version", header: "Prompt Version", cell: ({ row }) => row.original.prompt_version ?? <span className="text-muted-foreground">—</span> },
  { accessorKey: "code_version", header: "Code Version", cell: ({ row }) => row.original.code_version ? <span className="font-mono text-xs">{row.original.code_version.slice(0, 8)}</span> : <span className="text-muted-foreground">—</span> },
];

// ─── Usage columns ────────────────────────────────────────────────
const usageColumns: ColumnDef<UsageRow, unknown>[] = [
  { accessorKey: "provider", header: "Provider" },
  { accessorKey: "route", header: "Route" },
  { accessorKey: "usage_kind", header: "Kind" },
  { accessorKey: "request_count", header: "Requests", cell: ({ row }) => <span className="tabular-nums">{row.original.request_count}</span> },
  { accessorKey: "credit_count", header: "Credits", cell: ({ row }) => <span className="tabular-nums">{row.original.credit_count}</span> },
  { accessorKey: "prompt_tokens", header: "Prompt Tokens", cell: ({ row }) => <span className="tabular-nums">{row.original.prompt_tokens.toLocaleString()}</span> },
  { accessorKey: "completion_tokens", header: "Completion Tokens", cell: ({ row }) => <span className="tabular-nums">{row.original.completion_tokens.toLocaleString()}</span> },
  { accessorKey: "reasoning_tokens", header: "Reasoning Tokens", cell: ({ row }) => <span className="tabular-nums">{row.original.reasoning_tokens.toLocaleString()}</span> },
  { accessorKey: "cached_tokens", header: "Cached Tokens", cell: ({ row }) => <span className="tabular-nums">{row.original.cached_tokens.toLocaleString()}</span> },
  { accessorKey: "billed_cost_micros", header: "Billed Cost", cell: ({ row }) => <MicroDollars value={row.original.billed_cost_micros} /> },
  { accessorKey: "nominal_cost_micros", header: "Nominal Cost", cell: ({ row }) => <MicroDollars value={row.original.nominal_cost_micros} /> },
  { accessorKey: "average_latency_ms", header: "Avg Latency", cell: ({ row }) => row.original.average_latency_ms != null ? <span className="tabular-nums">{row.original.average_latency_ms.toFixed(0)}ms</span> : <span className="text-muted-foreground">—</span> },
  { accessorKey: "last_used_at", header: "Last Used", cell: ({ row }) => <RelativeTime date={row.original.last_used_at} /> },
];

// ─── Agent Detail Page ────────────────────────────────────────────
export default function AgentDetailPage() {
  const { agentId } = useParams<{ agentId: string }>();
  const [posOffset, setPosOffset] = useState(0);
  const [tradeOffset, setTradeOffset] = useState(0);
  const [settOffset, setSettOffset] = useState(0);
  const [rejOffset, setRejOffset] = useState(0);
  const [cycleOffset, setCycleOffset] = useState(0);
  const [usageOffset, setUsageOffset] = useState(0);

  const { data: leaderboard, isLoading: lbLoading } = useLeaderboard({ limit: 500 });
  const agent = leaderboard?.find((r) => r.agent_id === agentId);

  const { data: positions, isLoading: posLoading } = usePositions({ agent_id: agentId, limit: LIMIT, offset: posOffset });
  const { data: trades, isLoading: tradeLoading } = useTrades({ agent_id: agentId, limit: LIMIT, offset: tradeOffset });
  const { data: settlements, isLoading: settLoading } = useSettlements({ agent_id: agentId, limit: LIMIT, offset: settOffset });
  const { data: rejections, isLoading: rejLoading } = useRejections({ agent_id: agentId, limit: LIMIT, offset: rejOffset });
  const { data: cycles, isLoading: cycleLoading } = useCycles({ agent_id: agentId, limit: LIMIT, offset: cycleOffset });
  const { data: usage, isLoading: usageLoading } = useUsage({ agent_id: agentId, limit: LIMIT, offset: usageOffset });

  if (!agentId) return <div className="text-muted-foreground">No agent ID provided.</div>;

  return (
    <div>
      <PageHeader
        title={agent?.agent_name ?? agentId}
        description={lbLoading ? "Loading…" : agent?.model_label ?? "Unknown model"}
        actions={agent ? <PauseButton agentId={agent.agent_id} isPaused={!!agent.paused_at} /> : undefined}
      />

      {/* Summary Card */}
      <Card className="mb-6 border-[#1e2a38] bg-[#18202b]">
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">Agent Summary</CardTitle>
        </CardHeader>
        <CardContent>
          {lbLoading ? (
            <div className="text-muted-foreground">Loading…</div>
          ) : agent ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div>
                <span className="text-xs text-muted-foreground">Agent Name</span>
                <p className="text-sm font-medium text-[#eef3f8]">{agent.agent_name}</p>
              </div>
              <div>
                <span className="text-xs text-muted-foreground">Model</span>
                <p className="text-sm font-medium text-[#eef3f8]">{agent.model_label}</p>
              </div>
              <div>
                <span className="text-xs text-muted-foreground">Run ID</span>
                <p className="font-mono text-xs text-[#eef3f8]">{agent.run_id}</p>
              </div>
              <div>
                <span className="text-xs text-muted-foreground">Account Value</span>
                <p className="text-sm font-medium"><MicroDollars value={agent.account_value_micros} /></p>
              </div>
              <div>
                <span className="text-xs text-muted-foreground">Realized PnL</span>
                <p className="text-sm font-medium"><MicroDollars value={agent.realized_pnl_micros} showSign /></p>
              </div>
              <div>
                <span className="text-xs text-muted-foreground">Unrealized PnL</span>
                <p className="text-sm font-medium"><MicroDollars value={agent.unrealized_pnl_micros} showSign /></p>
              </div>
              <div>
                <span className="text-xs text-muted-foreground">Total PnL</span>
                <p className="text-sm font-medium"><MicroDollars value={agent.total_pnl_micros} showSign /></p>
              </div>
              <div>
                <span className="text-xs text-muted-foreground">Drawdown</span>
                <p className="text-sm font-medium tabular-nums">
                  {agent.drawdown_fraction != null ? `${(agent.drawdown_fraction * 100).toFixed(1)}%` : "—"}
                </p>
              </div>
            </div>
          ) : (
            <p className="text-muted-foreground">Agent not found in leaderboard.</p>
          )}
        </CardContent>
      </Card>

      {/* Tabs */}
      <Tabs defaultValue="positions">
        <TabsList className="mb-4 bg-[#18202b]">
          <TabsTrigger value="positions">Positions</TabsTrigger>
          <TabsTrigger value="trades">Trades</TabsTrigger>
          <TabsTrigger value="settlements">Settlements</TabsTrigger>
          <TabsTrigger value="rejections">Rejections</TabsTrigger>
          <TabsTrigger value="cycles">Cycles</TabsTrigger>
          <TabsTrigger value="usage">Usage</TabsTrigger>
        </TabsList>

        <TabsContent value="positions">
          <DataTable
            data={positions ?? []}
            columns={positionColumns}
            isLoading={posLoading}
            pagination={{ limit: LIMIT, offset: posOffset, onPageChange: setPosOffset }}
          />
        </TabsContent>

        <TabsContent value="trades">
          <DataTable
            data={trades ?? []}
            columns={tradeColumns}
            isLoading={tradeLoading}
            pagination={{ limit: LIMIT, offset: tradeOffset, onPageChange: setTradeOffset }}
          />
        </TabsContent>

        <TabsContent value="settlements">
          <DataTable
            data={settlements ?? []}
            columns={settlementColumns}
            isLoading={settLoading}
            pagination={{ limit: LIMIT, offset: settOffset, onPageChange: setSettOffset }}
          />
        </TabsContent>

        <TabsContent value="rejections">
          <DataTable
            data={rejections ?? []}
            columns={rejectionColumns}
            isLoading={rejLoading}
            pagination={{ limit: LIMIT, offset: rejOffset, onPageChange: setRejOffset }}
          />
        </TabsContent>

        <TabsContent value="cycles">
          <DataTable
            data={cycles ?? []}
            columns={cycleColumns}
            isLoading={cycleLoading}
            pagination={{ limit: LIMIT, offset: cycleOffset, onPageChange: setCycleOffset }}
          />
        </TabsContent>

        <TabsContent value="usage">
          <DataTable
            data={usage ?? []}
            columns={usageColumns}
            isLoading={usageLoading}
            pagination={{ limit: LIMIT, offset: usageOffset, onPageChange: setUsageOffset }}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}