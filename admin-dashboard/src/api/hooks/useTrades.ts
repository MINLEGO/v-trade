import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { TradeRow, AgentFilterParams } from "@/api/types";

export function useTrades(params: AgentFilterParams = {}) {
  return useQuery({
    queryKey: ["trades", params],
    queryFn: () =>
      apiGet<TradeRow[]>("/admin/trades", params as Record<string, string | number>),
  });
}
