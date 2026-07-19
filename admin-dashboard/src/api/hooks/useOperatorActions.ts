import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { OperatorActionRow, PageParams } from "@/api/types";

export function useOperatorActions(params: PageParams = {}) {
  return useQuery({
    queryKey: ["operator-actions", params],
    queryFn: () =>
      apiGet<OperatorActionRow[]>("/admin/operator-actions", params as Record<string, string | number>),
  });
}
