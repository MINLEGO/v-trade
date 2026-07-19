import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/api/client";
import type { LeaderboardRow, PageParams } from "@/api/types";

export function useLeaderboard(params: PageParams = {}) {
  return useQuery({
    queryKey: ["leaderboard", params],
    queryFn: () =>
      apiGet<LeaderboardRow[]>("/admin/leaderboard", params as Record<string, string | number>),
  });
}
