import { Routes, Route, Navigate, Outlet } from "react-router-dom";
import { useAuthStore } from "@/store/auth";
import { AppShell } from "@/components/layout/AppShell";

// ─── Page imports ─────────────────────────────────────────────────
import LoginPage from "@/pages/LoginPage";
import OverviewPage from "@/pages/OverviewPage";
import LeaderboardPage from "@/pages/LeaderboardPage";
import AgentDetailPage from "@/pages/AgentDetailPage";
import PositionsPage from "@/pages/PositionsPage";
import TradesPage from "@/pages/TradesPage";
import SettlementsPage from "@/pages/SettlementsPage";
import RejectionsPage from "@/pages/RejectionsPage";
import CyclesPage from "@/pages/CyclesPage";
import UsagePage from "@/pages/UsagePage";
import FreshnessPage from "@/pages/FreshnessPage";
import AlertsPage from "@/pages/AlertsPage";
import ConfigVersionsPage from "@/pages/ConfigVersionsPage";
import AuditLogPage from "@/pages/AuditLogPage";
import HealthPage from "@/pages/HealthPage";

// ─── Auth guard layout ────────────────────────────────────────────
function RequireAuth() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <Outlet />;
}

// ─── Root layout (AppShell) ───────────────────────────────────────
function DashboardLayout() {
  return <AppShell />;
}

// ─── App ──────────────────────────────────────────────────────────
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      {/* Protected dashboard routes */}
      <Route element={<RequireAuth />}>
        <Route element={<DashboardLayout />}>
          <Route index element={<OverviewPage />} />
          <Route path="leaderboard" element={<LeaderboardPage />} />
          <Route path="agents/:agentId" element={<AgentDetailPage />} />
          <Route path="positions" element={<PositionsPage />} />
          <Route path="trades" element={<TradesPage />} />
          <Route path="settlements" element={<SettlementsPage />} />
          <Route path="rejections" element={<RejectionsPage />} />
          <Route path="cycles" element={<CyclesPage />} />
          <Route path="usage" element={<UsagePage />} />
          <Route path="freshness" element={<FreshnessPage />} />
          <Route path="alerts" element={<AlertsPage />} />
          <Route path="config-versions" element={<ConfigVersionsPage />} />
          <Route path="audit-log" element={<AuditLogPage />} />
          <Route path="health" element={<HealthPage />} />
        </Route>
      </Route>

      {/* Catch-all redirect */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}