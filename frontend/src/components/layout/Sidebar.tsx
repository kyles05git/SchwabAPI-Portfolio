import type { ViewKey } from "@/App";
import { getStoredTheme, setTheme } from "@/lib/theme";
import { useState } from "react";
import {
  FlaskConical,
  Briefcase,
  LineChart,
  Shield,
  Activity,
  Sun,
  Moon,
  Monitor,
} from "lucide-react";

interface SidebarProps {
  view: ViewKey;
  onSelectView: (view: ViewKey) => void;
  liveEnabled: boolean;
  className?: string;
}

const NAV_ITEMS: { key: ViewKey; label: string; icon: typeof FlaskConical }[] = [
  { key: "cohort", label: "Paper Cohort", icon: FlaskConical },
  { key: "portfolio", label: "Portfolio", icon: Briefcase },
  { key: "research", label: "Research", icon: LineChart },
  { key: "operations", label: "Operations", icon: Shield },
];

type ThemeOption = "dark" | "light" | "system";

export function Sidebar({ view, onSelectView, liveEnabled, className = "" }: SidebarProps) {
  const [currentTheme, setCurrentTheme] = useState<ThemeOption>(getStoredTheme());

  function cycleTheme() {
    const order: ThemeOption[] = ["dark", "light", "system"];
    const next = order[(order.indexOf(currentTheme) + 1) % order.length];
    setTheme(next);
    setCurrentTheme(next);
  }

  const ThemeIcon = currentTheme === "dark" ? Moon : currentTheme === "light" ? Sun : Monitor;

  return (
    <aside
      className={`flex w-56 flex-col border-r border-sidebar-border bg-sidebar ${className}`}
      aria-label="Main navigation"
    >
      {/* Brand */}
      <div className="flex items-center gap-2.5 border-b border-sidebar-border px-4 py-4">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-sidebar-primary/10">
          <Activity className="h-4 w-4 text-sidebar-primary" />
        </div>
        <span className="text-sm font-semibold tracking-tight text-sidebar-foreground">
          SchwabTrader
        </span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-2 py-3 space-y-0.5">
        {NAV_ITEMS.map((item) => {
          const active = item.key === view;
          const Icon = item.icon;
          const disabled = item.key === "portfolio" && !liveEnabled;

          return (
            <button
              key={item.key}
              type="button"
              disabled={disabled}
              aria-current={active ? "page" : undefined}
              onClick={() => onSelectView(item.key)}
              className={`focus-ring flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                active
                  ? "bg-sidebar-accent text-sidebar-foreground"
                  : "text-sidebar-foreground/70 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
              } ${disabled ? "opacity-40 cursor-not-allowed" : "cursor-pointer"}`}
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span>{item.label}</span>
              {disabled && (
                <span className="ml-auto text-[10px] text-muted-foreground">offline</span>
              )}
            </button>
          );
        })}
      </nav>

      {/* Theme toggle */}
      <div className="border-t border-sidebar-border px-3 py-3">
        <button
          type="button"
          onClick={cycleTheme}
          className="focus-ring flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-sm text-sidebar-foreground/70 transition-colors hover:bg-sidebar-accent/50 hover:text-sidebar-foreground cursor-pointer"
          aria-label={`Theme: ${currentTheme}. Click to change.`}
        >
          <ThemeIcon className="h-4 w-4 shrink-0" />
          <span className="capitalize">{currentTheme}</span>
        </button>
      </div>
    </aside>
  );
}
