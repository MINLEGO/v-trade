import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { SettlementRow, AgentFilterParams } from "@/api/types";

export function useSettlements(params: AgentFilterParams = {}) {
  return useQuery({
    queryKey: ["settlements", params],
    queryFn: () =>
      apiGet<SettlementRow[]>("/admin/settlements", params as Record<string, string | number>),
  });
}
