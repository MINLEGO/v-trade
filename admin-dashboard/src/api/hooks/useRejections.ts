import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { RejectionRow, AgentFilterParams } from "@/api/types";

export function useRejections(params: AgentFilterParams = {}) {
  return useQuery({
    queryKey: ["rejections", params],
    queryFn: () =>
      apiGet<RejectionRow[]>("/admin/rejections", params as Record<string, string | number>),
  });
}
