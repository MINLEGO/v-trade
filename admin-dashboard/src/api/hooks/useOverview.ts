import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { Overview } from "@/api/types";

export function useOverview() {
  return useQuery({
    queryKey: ["overview"],
    queryFn: () => apiGet<Overview>("/admin/overview"),
  });
}
