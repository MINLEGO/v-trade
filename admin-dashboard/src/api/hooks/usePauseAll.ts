import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost } from "@/api/client";

export function usePauseAll() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<unknown>("/admin/control/pause"),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
    },
  });
}
