import type { ConnectionState } from "@/lib/useWebSocket";
import {
  Wifi,
  WifiOff,
  RefreshCw,
  Timer,
  Menu,
  ShieldAlert,
} from "lucide-react";

interface StatusBarProps {
  account: string;
  connection: ConnectionState;
  lastRefresh: Date | null;
  error: string | null;
  refreshSeconds: number;
  liveEnabled: boolean;
  killEngaged: boolean;
  onRefresh: () => void;
  onMenuToggle: () => void;
}

const CONNECTION_META: Record<
  ConnectionState,
  { label: string; icon: typeof Wifi; color: string }
> = {
  live: { label: "Live", icon: Wifi, color: "text-profit" },
  polling: { label: "Polling", icon: Timer, color: "text-muted-foreground" },
  reconnecting: { label: "Reconnecting", icon: RefreshCw, color: "text-warn" },
  offline: { label: "Offline", icon: WifiOff, color: "text-loss" },
};

export function StatusBar({
  account,
  connection,
  lastRefresh,
  error,
  refreshSeconds,
  liveEnabled,
  killEngaged,
  onRefresh,
  onMenuToggle,
}: StatusBarProps) {
  const connMeta = CONNECTION_META[connection];
  const ConnIcon = connMeta.icon;
  const updated = lastRefresh
    ? lastRefresh.toLocaleTimeString(undefined, { hour12: false })
    : null;

  return (
    <header className="sticky top-0 z-40 flex items-center gap-2 border-b border-border bg-background/95 backdrop-blur-sm px-4 py-2.5 sm:px-5">
      {/* Mobile menu button */}
      <button
        type="button"
        onClick={onMenuToggle}
        className="focus-ring rounded-md p-1.5 text-muted-foreground hover:text-foreground lg:hidden cursor-pointer"
        aria-label="Open navigation"
      >
        <Menu className="h-5 w-5" />
      </button>

      {/* Account badge */}
      {account && (
        <span className="rounded-md border border-border bg-secondary px-2 py-0.5 text-xs font-medium text-secondary-foreground">
          {account}
        </span>
      )}

      {/* Read-only badge */}
      <span className="hidden sm:inline-flex rounded-md border border-primary/20 bg-primary/5 px-2 py-0.5 text-[11px] font-medium text-primary">
        Read-only
      </span>

      {/* Live enabled indicator */}
      {!liveEnabled && (
        <span className="hidden md:inline-flex rounded-md border border-warn/20 bg-warn/5 px-2 py-0.5 text-[11px] font-medium text-warn">
          No-live
        </span>
      )}

      {/* Kill switch indicator (compact, in status bar) */}
      {killEngaged && (
        <span className="inline-flex items-center gap-1 rounded-md border border-loss/30 bg-loss/10 px-2 py-0.5 text-[11px] font-semibold text-loss">
          <ShieldAlert className="h-3 w-3" />
          HALTED
        </span>
      )}

      {/* Spacer */}
      <div className="flex-1" />

      {/* Connection state */}
      <span
        className={`flex items-center gap-1.5 text-xs ${connMeta.color}`}
        title={
          connection === "polling"
            ? `Polling every ${refreshSeconds}s`
            : connMeta.label
        }
      >
        <ConnIcon
          className={`h-3.5 w-3.5 ${connection === "reconnecting" ? "animate-spin" : ""}`}
        />
        <span className="hidden sm:inline">{connMeta.label}</span>
      </span>

      {/* Last refresh */}
      {updated && !error && (
        <span className="hidden md:flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-profit animate-pulse-dot" />
          {updated}
        </span>
      )}

      {/* Error indicator */}
      {error && (
        <span className="text-xs text-loss truncate max-w-40" title={error}>
          {error}
        </span>
      )}

      {/* Refresh button */}
      <button
        type="button"
        onClick={onRefresh}
        className="focus-ring rounded-md p-1.5 text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
        aria-label="Refresh data"
        title="Refresh data now"
      >
        <RefreshCw className="h-4 w-4" />
      </button>
    </header>
  );
}
