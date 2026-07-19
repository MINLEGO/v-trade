import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { HealthLive } from "@/api/types";

export function useHealthLive() {
  return useQuery({
    queryKey: ["health", "live"],
    queryFn: () => apiGet<HealthLive>("/health/live"),
    refetchInterval: 30_000,
  });
}
