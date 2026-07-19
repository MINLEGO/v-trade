import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { ConfigVersionRow, PageParams } from "@/api/types";

export function useConfigVersions(params: PageParams = {}) {
  return useQuery({
    queryKey: ["config-versions", params],
    queryFn: () =>
      apiGet<ConfigVersionRow[]>("/admin/config-versions", params as Record<string, string | number>),
  });
}
