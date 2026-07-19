import { create } from "zustand";

interface FiltersState {
  selectedAgentId: string | null;
  setAgent: (id: string | null) => void;
  clearAgent: () => void;
}

export const useFiltersStore = create<FiltersState>()((set) => ({
  selectedAgentId: null,
  setAgent: (id: string | null) => set({ selectedAgentId: id }),
  clearAgent: () => set({ selectedAgentId: null }),
}));
