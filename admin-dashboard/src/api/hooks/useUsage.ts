import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { UsageRow, AgentFilterParams } from "@/api/types";

export function useUsage(params: AgentFilterParams = {}) {
  return useQuery({
    queryKey: ["usage", params],
    queryFn: () =>
      apiGet<UsageRow[]>("/admin/usage", params as Record<string, string | number>),
  });
}
