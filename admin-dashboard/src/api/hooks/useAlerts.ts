import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { AlertRow, PageParams } from "@/api/types";

export function useAlerts(params: PageParams = {}) {
  return useQuery({
    queryKey: ["alerts", params],
    queryFn: () =>
      apiGet<AlertRow[]>("/admin/alerts", params as Record<string, string | number>),
  });
}
