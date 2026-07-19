import { create } from "zustand";

interface AuthState {
  secret: string | null;
  operatorId: string | null;
  isAuthenticated: boolean;
  login: (secret: string, operatorId: string) => void;
  logout: () => void;
}

const STORAGE_SECRET_KEY = "vtrade_admin_secret";
const STORAGE_OPERATOR_KEY = "vtrade_admin_operator_id";

function loadFromSession(): Pick<AuthState, "secret" | "operatorId"> {
  try {
    const secret = sessionStorage.getItem(STORAGE_SECRET_KEY);
    const operatorId = sessionStorage.getItem(STORAGE_OPERATOR_KEY);
    return { secret, operatorId };
  } catch {
    return { secret: null, operatorId: null };
  }
}

const initial = loadFromSession();

export const useAuthStore = create<AuthState>()((set) => ({
  secret: initial.secret,
  operatorId: initial.operatorId,
  isAuthenticated: initial.secret !== null,
  login: (secret: string, operatorId: string) => {
    sessionStorage.setItem(STORAGE_SECRET_KEY, secret);
    sessionStorage.setItem(STORAGE_OPERATOR_KEY, operatorId);
    set({ secret, operatorId, isAuthenticated: true });
  },
  logout: () => {
    sessionStorage.removeItem(STORAGE_SECRET_KEY);
    sessionStorage.removeItem(STORAGE_OPERATOR_KEY);
    set({ secret: null, operatorId: null, isAuthenticated: false });
  },
}));
