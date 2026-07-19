import { NavLink } from "react-router-dom";
import {
  LayoutDashboard,
  Trophy,
  Layers,
  ArrowLeftRight,
  Landmark,
  XCircle,
  RefreshCw,
  DollarSign,
  Activity,
  Bell,
  FileCode,
  ClipboardList,
  HeartPulse,
  Menu,
  X,
} from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

const navItems: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard },
  { to: "/leaderboard", label: "Leaderboard", icon: Trophy },
  { to: "/positions", label: "Positions", icon: Layers },
  { to: "/trades", label: "Trades", icon: ArrowLeftRight },
  { to: "/settlements", label: "Settlements", icon: Landmark },
  { to: "/rejections", label: "Rejections", icon: XCircle },
  { to: "/cycles", label: "Cycles", icon: RefreshCw },
  { to: "/usage", label: "Usage & Cost", icon: DollarSign },
  { to: "/freshness", label: "Freshness", icon: Activity },
  { to: "/alerts", label: "Alerts", icon: Bell },
  { to: "/config-versions", label: "Config & Versions", icon: FileCode },
  { to: "/audit-log", label: "Audit Log", icon: ClipboardList },
  { to: "/health", label: "Health", icon: HeartPulse },
];

export function Sidebar() {
  const [open, setOpen] = useState(false);

  return (
    <>
      {/* Mobile hamburger */}
      <button
        onClick={() => setOpen(true)}
        className="fixed left-4 top-4 z-50 rounded-md p-2 text-muted-foreground hover:bg-[#18202b] hover:text-[#eef3f8] lg:hidden"
        aria-label="Open menu"
      >
        <Menu className="size-5" />
      </button>

      {/* Overlay */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/60 lg:hidden"
          onClick={() => setOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-60 flex-col border-r border-[#1e2a38] bg-[#0d1117] transition-transform duration-200 lg:static lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center justify-between px-4 py-5">
          <span className="text-lg font-bold text-[#8fd3ff]">V-Trade</span>
          <button
            onClick={() => setOpen(false)}
            className="rounded-md p-1 text-muted-foreground hover:bg-[#18202b] hover:text-[#eef3f8] lg:hidden"
            aria-label="Close menu"
          >
            <X className="size-4" />
          </button>
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2 pb-4">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-[#18202b] text-[#8fd3ff]"
                    : "text-muted-foreground hover:bg-[#18202b] hover:text-[#eef3f8]",
                )
              }
            >
              <item.icon className="size-4 shrink-0" />
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
    </>
  );
}
