import { useState } from "react";
import { usePauseAll } from "@/api/hooks/usePauseAll";
import { useResumeAll } from "@/api/hooks/useResumeAll";
import { usePauseAgent } from "@/api/hooks/usePauseAgent";
import { useResumeAgent } from "@/api/hooks/useResumeAgent";
import { useAuthStore } from "@/store/auth";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Pause, Play, Loader2 } from "lucide-react";

interface PauseButtonProps {
  agentId?: string;
  isPaused: boolean;
}

export function PauseButton({ agentId, isPaused }: PauseButtonProps) {
  const [open, setOpen] = useState(false);
  const operatorId = useAuthStore((s) => s.operatorId);

  const pauseAll = usePauseAll();
  const resumeAll = useResumeAll();
  const pauseAgent = usePauseAgent(agentId ?? "");
  const resumeAgent = useResumeAgent(agentId ?? "");

  const mutation = agentId
    ? isPaused
      ? resumeAgent
      : pauseAgent
    : isPaused
      ? resumeAll
      : pauseAll;

  const action = isPaused ? "Resume" : "Pause";
  const target = agentId ? "this agent" : "all agents";
  const isPending = mutation.isPending;

  function handleConfirm() {
    if (!operatorId) return;
    mutation.mutate(undefined, {
      onSuccess: () => setOpen(false),
    });
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant={isPaused ? "default" : "destructive"}
          size="sm"
          disabled={!operatorId}
        >
          {isPending ? (
            <Loader2 className="size-4 animate-spin" />
          ) : isPaused ? (
            <Play className="size-4" />
          ) : (
            <Pause className="size-4" />
          )}
          <span className="hidden sm:inline">
            {action}
            {agentId ? "" : " All"}
          </span>
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {action} {target}?
          </DialogTitle>
          <DialogDescription>
            Are you sure you want to {action.toLowerCase()} {target}? This will
            take effect immediately.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            variant={isPaused ? "default" : "destructive"}
            onClick={handleConfirm}
            disabled={isPending}
          >
            {isPending && <Loader2 className="mr-2 size-4 animate-spin" />}
            Confirm {action}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
