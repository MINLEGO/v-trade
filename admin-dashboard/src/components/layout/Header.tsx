import { useHealthLive } from "@/api/hooks/useHealthLive";
import { useOverview } from "@/api/hooks/useOverview";
import { useAuthStore } from "@/store/auth";
import { AgentFilter } from "@/components/AgentFilter";
import { PauseButton } from "@/components/PauseButton";
import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";

export function Header() {
  const { data: health } = useHealthLive();
  const { data: overview } = useOverview();
  const logout = useAuthStore((s) => s.logout);

  const isHealthy = health?.status === "ok" || health?.status === "alive";
  const isGloballyPaused = overview?.controls.globally_paused ?? false;

  return (
    <header className="flex items-center justify-between gap-4 border-b border-[#1e2a38] bg-[#10141b] px-6 py-3">
      <h1 className="text-lg font-semibold text-[#eef3f8]">V-Trade Admin</h1>

      <div className="flex items-center gap-4">
        {/* Health indicator */}
        <div className="flex items-center gap-2 text-sm">
          <span
            className={`size-2.5 rounded-full ${isHealthy ? "bg-green-500" : "bg-red-500"}`}
          />
          <span className="hidden text-muted-foreground sm:inline">
            {isHealthy ? "Healthy" : "Down"}
          </span>
        </div>

        {/* Agent filter */}
        <AgentFilter />

        {/* Global pause/resume */}
        <PauseButton isPaused={isGloballyPaused} />

        {/* Logout */}
        <Button
          variant="ghost"
          size="sm"
          onClick={logout}
          className="text-muted-foreground hover:text-[#eef3f8]"
        >
          <LogOut className="size-4" />
          <span className="hidden sm:inline">Logout</span>
        </Button>
      </div>
    </header>
  );
}
