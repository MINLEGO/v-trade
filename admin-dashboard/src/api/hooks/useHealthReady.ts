import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { HealthReady } from "@/api/types";

export function useHealthReady() {
  return useQuery({
    queryKey: ["health", "ready"],
    queryFn: () => apiGet<HealthReady>("/health/ready"),
    refetchInterval: 30_000,
  });
}
