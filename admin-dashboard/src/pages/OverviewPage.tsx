import { Link } from "react-router-dom";
import { useOverview } from "@/api/hooks/useOverview";
import { useLeaderboard } from "@/api/hooks/useLeaderboard";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { KPICard } from "@/components/KPICard";
import { StatusBadge } from "@/components/StatusBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { MicroDollars } from "@/components/MicroDollars";
import { PauseButton } from "@/components/PauseButton";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Activity,
  Users,
  AlertTriangle,
  RefreshCw,
} from "lucide-react";

export default function OverviewPage() {
  const { data: overview, isLoading: overviewLoading } = useOverview();
  const { data: leaderboard, isLoading: lbLoading } = useLeaderboard({
    limit: 10,
  });

  return (
    <div>
      <PageHeader title="Overview" description="Summary of V-Trade system status" />

      {/* KPI Cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {overviewLoading ? (
          Array.from({ length: 4 }).map((_, i) => (
            <Card key={i} className="border-[#1e2a38] bg-[#18202b]">
              <CardContent className="pt-6">
                <Skeleton className="h-4 w-24" />
                <Skeleton className="mt-2 h-8 w-16" />
                <Skeleton className="mt-1 h-3 w-32" />
              </CardContent>
            </Card>
          ))
        ) : overview ? (
          <>
            <KPICard
              title="Experiment Runs"
              value={overview.runs.runs}
              description={`${overview.runs.running_runs} running, ${overview.runs.paused_runs} paused`}
              icon={<Activity className="size-4" />}
            />
            <KPICard
              title="Agents"
              value={overview.agents.agents}
              description={`${overview.agents.paused_agents} paused`}
              icon={<Users className="size-4" />}
            />
            <KPICard
              title="Open Alerts"
              value={overview.alerts.open_alerts}
              trend={overview.alerts.open_alerts > 0 ? "up" : "neutral"}
              description={
                overview.alerts.latest_alert_at
                  ? `Latest: ${new Date(overview.alerts.latest_alert_at).toLocaleString()}`
                  : "No alerts"
              }
              icon={<AlertTriangle className="size-4" />}
            />
            <KPICard
              title="Cycles"
              value={
                overview.cycles.running_cycles + overview.cycles.failed_cycles
              }
              description={`${overview.cycles.running_cycles} running, ${overview.cycles.failed_cycles} failed`}
              icon={<RefreshCw className="size-4" />}
            />
          </>
        ) : null}
      </div>

      {/* System Controls */}
      {overview && (
        <Card className="mt-6 border-[#1e2a38] bg-[#18202b]">
          <CardHeader>
            <CardTitle className="text-sm font-medium text-muted-foreground">
              System Controls
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap items-center gap-4">
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted-foreground">Status:</span>
                <StatusBadge
                  status={
                    overview.controls.globally_paused ? "paused" : "running"
                  }
                />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-sm text-muted-foreground">
                  Last updated:
                </span>
                <RelativeTime date={overview.controls.updated_at} />
                <span className="text-sm text-muted-foreground">
                  by {overview.controls.updated_by}
                </span>
              </div>
              <div className="ml-auto">
                <PauseButton
                  isPaused={overview.controls.globally_paused}
                />
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Quick Leaderboard */}
      <Card className="mt-6 border-[#1e2a38] bg-[#18202b]">
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Top Agents
          </CardTitle>
          <Link
            to="/leaderboard"
            className="text-sm text-[#8fd3ff] hover:underline"
          >
            View all →
          </Link>
        </CardHeader>
        <CardContent>
          <div className="rounded-md border border-[#1e2a38]">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Rank</TableHead>
                  <TableHead>Agent</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead className="text-right">Account Value</TableHead>
                  <TableHead className="text-right">Total PnL</TableHead>
                  <TableHead className="text-right">Drawdown</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {lbLoading ? (
                  Array.from({ length: 5 }).map((_, i) => (
                    <TableRow key={i}>
                      {Array.from({ length: 6 }).map((_, j) => (
                        <TableCell key={j}>
                          <Skeleton className="h-4 w-full" />
                        </TableCell>
                      ))}
                    </TableRow>
                  ))
                ) : leaderboard && leaderboard.length > 0 ? (
                  leaderboard.map((row, idx) => (
                    <TableRow key={row.agent_id}>
                      <TableCell className="tabular-nums">
                        {idx + 1}
                      </TableCell>
                      <TableCell>
                        <Link
                          to={`/agents/${row.agent_id}`}
                          className="text-[#8fd3ff] hover:underline"
                        >
                          {row.agent_name}
                        </Link>
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {row.model_label}
                      </TableCell>
                      <TableCell className="text-right">
                        <MicroDollars value={row.account_value_micros} />
                      </TableCell>
                      <TableCell className="text-right">
                        <MicroDollars
                          value={row.total_pnl_micros}
                          showSign
                        />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {row.drawdown_fraction != null
                          ? `${(row.drawdown_fraction * 100).toFixed(1)}%`
                          : "—"}
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell
                      colSpan={6}
                      className="h-24 text-center text-muted-foreground"
                    >
                      No agents found.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}