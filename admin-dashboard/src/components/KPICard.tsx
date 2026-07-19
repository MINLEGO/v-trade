import type { ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";

interface KPICardProps {
  title: string;
  value: string | number;
  description?: string;
  icon?: ReactNode;
  trend?: "up" | "down" | "neutral";
  trendValue?: string;
}

const trendConfig = {
  up: { icon: TrendingUp, color: "text-green-500" },
  down: { icon: TrendingDown, color: "text-red-500" },
  neutral: { icon: Minus, color: "text-muted-foreground" },
} as const;

export function KPICard({
  title,
  value,
  description,
  icon,
  trend,
  trendValue,
}: KPICardProps) {
  const TrendIcon = trend ? trendConfig[trend].icon : null;
  const trendColor = trend ? trendConfig[trend].color : null;

  return (
    <Card className="border-[#1e2a38] bg-[#18202b]">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
        {icon && <div className="text-muted-foreground">{icon}</div>}
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold text-[#eef3f8]">{value}</div>
        {(description || trend) && (
          <div className="mt-1 flex items-center gap-1 text-xs">
            {trend && TrendIcon && (
              <span className={cn("flex items-center gap-0.5", trendColor)}>
                <TrendIcon className="size-3" />
                {trendValue}
              </span>
            )}
            {description && (
              <span className="text-muted-foreground">{description}</span>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
