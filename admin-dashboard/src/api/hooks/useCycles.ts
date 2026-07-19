import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { CycleRow, AgentFilterParams } from "@/api/types";

export function useCycles(params: AgentFilterParams = {}) {
  return useQuery({
    queryKey: ["cycles", params],
    queryFn: () =>
      apiGet<CycleRow[]>("/admin/cycles", params as Record<string, string | number>),
  });
}
