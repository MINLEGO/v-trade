import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { FreshnessRow } from "@/api/types";

export function useFreshness() {
  return useQuery({
    queryKey: ["freshness"],
    queryFn: () => apiGet<FreshnessRow[]>("/admin/freshness"),
  });
}
