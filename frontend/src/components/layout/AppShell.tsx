import { useState, type ReactNode } from "react";
import type { DashboardData } from "@/lib/types";
import type { ViewKey } from "@/App";
import type { ConnectionState } from "@/lib/useWebSocket";
import { Sidebar } from "./Sidebar";
import { StatusBar } from "./StatusBar";
import { KillSwitchBar } from "../dashboard/KillSwitchBar";

interface AppShellProps {
  data: DashboardData;
  view: ViewKey;
  onSelectView: (view: ViewKey) => void;
  connection: ConnectionState;
  lastRefresh: Date | null;
  error: string | null;
  refreshSeconds: number;
  onRefresh: () => void;
  children: ReactNode;
}

export function AppShell({
  data,
  view,
  onSelectView,
  connection,
  lastRefresh,
  error,
  refreshSeconds,
  onRefresh,
  children,
}: AppShellProps) {
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="flex min-h-screen bg-background">
      {/* Desktop sidebar */}
      <Sidebar
        view={view}
        onSelectView={(v) => {
          onSelectView(v);
          setSidebarOpen(false);
        }}
        liveEnabled={data.live_enabled}
        className="hidden lg:flex"
      />

      {/* Mobile drawer overlay */}
      {sidebarOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div
            className="absolute inset-0 bg-background/80 backdrop-blur-sm"
            onClick={() => setSidebarOpen(false)}
            aria-hidden
          />
          <Sidebar
            view={view}
            onSelectView={(v) => {
              onSelectView(v);
              setSidebarOpen(false);
            }}
            liveEnabled={data.live_enabled}
            className="relative z-10 animate-slide-in-right"
          />
        </div>
      )}

      {/* Main content area */}
      <div className="flex flex-1 flex-col min-w-0">
        <StatusBar
          account={data.summary.account}
          connection={connection}
          lastRefresh={lastRefresh}
          error={error}
          refreshSeconds={refreshSeconds}
          liveEnabled={data.live_enabled}
          killEngaged={data.safety.kill_engaged}
          onRefresh={onRefresh}
          onMenuToggle={() => setSidebarOpen(true)}
        />

        <KillSwitchBar view={data.safety} onKilled={onRefresh} />

        <main className="flex-1 overflow-y-auto p-4 sm:p-5 lg:p-6">
          <div className="mx-auto max-w-[1400px] animate-fade-in">
            {children}
          </div>
        </main>

        <footer className="border-t border-border py-4 px-5 text-center">
          <p className="text-xs text-muted-foreground">
            Local dashboard · Read-only except the kill switch · API {data.api_version}
          </p>
        </footer>
      </div>
    </div>
  );
}
