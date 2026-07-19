import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { PositionRow, AgentFilterParams } from "@/api/types";

export function usePositions(params: AgentFilterParams = {}) {
  return useQuery({
    queryKey: ["positions", params],
    queryFn: () =>
      apiGet<PositionRow[]>("/admin/positions", params as Record<string, string | number>),
  });
}
