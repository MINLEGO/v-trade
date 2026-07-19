import { useHealthLive } from "@/api/hooks/useHealthLive";
import { useHealthReady } from "@/api/hooks/useHealthReady";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/StatusBadge";
import { RelativeTime } from "@/components/RelativeTime";
import { cn } from "@/lib/utils";

function LivenessCard({ isLoading }: { isLoading: boolean }) {
  const { data, isError } = useHealthLive();

  const isAlive = !!data && !isError;

  return (
    <Card className="border-[#1e2a38] bg-[#18202b]">
      <CardHeader>
        <CardTitle className="text-sm font-medium text-muted-foreground">
          Liveness
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-8 w-32" />
        ) : (
          <div className="flex items-center gap-3">
            <span
              className={cn(
                "inline-block size-4 rounded-full",
                isAlive ? "bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.5)]" : "bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.5)]",
              )}
            />
            <span className="text-lg font-semibold text-[#eef3f8]">
              {isAlive ? "Alive" : "Not Responding"}
            </span>
          </div>
        )}
        {data && (
          <p className="mt-2 text-xs text-muted-foreground">
            Last checked: <RelativeTime date={data.checked_at} />
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function ReadinessSubCheck({
  name,
  check,
}: {
  name: string;
  check: Record<string, unknown>;
}) {
  const status = (check.status as string) ?? "unknown";
  const entries = Object.entries(check).filter(([k]) => k !== "status");

  return (
    <Card className="border-[#1e2a38] bg-[#0d1117]">
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-[#eef3f8] capitalize">
            {name.replace(/_/g, " ")}
          </span>
          <StatusBadge status={status} />
        </div>
      </CardHeader>
      {entries.length > 0 && (
        <CardContent className="pt-0">
          <dl className="space-y-1">
            {entries.map(([key, value]) => (
              <div key={key} className="flex items-baseline gap-2 text-xs">
                <dt className="text-muted-foreground capitalize">
                  {key.replace(/_/g, " ")}:
                </dt>
                <dd className="font-mono text-[#eef3f8]">
                  {typeof value === "object"
                    ? JSON.stringify(value)
                    : String(value)}
                </dd>
              </div>
            ))}
          </dl>
        </CardContent>
      )}
    </Card>
  );
}

export default function HealthPage() {
  const { isLoading: liveLoading } = useHealthLive();
  const { data: readyData, isLoading: readyLoading } = useHealthReady();

  return (
    <div>
      <PageHeader
        title="System Health"
        description="Service health and readiness checks"
      />

      <div className="grid gap-4 sm:grid-cols-2 mb-6">
        <LivenessCard isLoading={liveLoading} />

        <Card className="border-[#1e2a38] bg-[#18202b]">
          <CardHeader>
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Readiness
            </CardTitle>
          </CardHeader>
          <CardContent>
            {readyLoading ? (
              <Skeleton className="h-8 w-32" />
            ) : readyData ? (
              <>
                <div className="flex items-center gap-2 mb-4">
                  <StatusBadge status={readyData.status} />
                  <span className="text-xs text-muted-foreground">
                    Checked: <RelativeTime date={readyData.checked_at} />
                  </span>
                </div>
                <div className="space-y-3">
                  {Object.entries(readyData.checks).map(([name, check]) =>
                    check ? (
                      <ReadinessSubCheck
                        key={name}
                        name={name}
                        check={check as Record<string, unknown>}
                      />
                    ) : null,
                  )}
                </div>
              </>
            ) : (
              <span className="text-muted-foreground">No data available</span>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
