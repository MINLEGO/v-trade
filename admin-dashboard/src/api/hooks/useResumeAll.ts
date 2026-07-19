import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost } from "@/api/client";

export function useResumeAll() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<unknown>("/admin/control/resume"),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
    },
  });
}
