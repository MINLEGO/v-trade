import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost } from "@/api/client";

export function useResumeAgent(agentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<unknown>(`/admin/agents/${agentId}/resume`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
      void queryClient.invalidateQueries({ queryKey: ["leaderboard"] });
    },
  });
}
