import { useFiltersStore } from "@/store/filters";
import { useLeaderboard } from "@/api/hooks/useLeaderboard";
import { Input } from "@/components/ui/input";
import { X } from "lucide-react";

export function AgentFilter() {
  const { selectedAgentId, setAgent, clearAgent } = useFiltersStore();
  const { data: leaderboard } = useLeaderboard({ limit: 500 });

  const agents = leaderboard
    ? leaderboard.map((row) => ({
        id: row.agent_id,
        name: row.agent_name,
      }))
    : [];

  // Deduplicate agents
  const uniqueAgents = agents.filter(
    (agent, index, arr) => arr.findIndex((a) => a.id === agent.id) === index,
  );

  return (
    <div className="flex items-center gap-2">
      <div className="relative">
        <Input
          list="agent-filter-list"
          placeholder="Filter agent…"
          value={selectedAgentId ?? ""}
          onChange={(e) => {
            const val = e.target.value;
            setAgent(val || null);
          }}
          className="h-8 w-48 bg-[#18202b] text-sm"
        />
        <datalist id="agent-filter-list">
          {uniqueAgents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.name}
            </option>
          ))}
        </datalist>
        {selectedAgentId && (
          <button
            onClick={clearAgent}
            className="absolute right-1 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground hover:text-[#eef3f8]"
            aria-label="Clear filter"
          >
            <X className="size-3" />
          </button>
        )}
      </div>
    </div>
  );
}
