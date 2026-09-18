import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import type { DashboardData } from "@/lib/types";
import {
  useWebSocket,
  connectionState,
  type ConnectionStatus,
  type ConnectionState,
} from "@/lib/useWebSocket";
import { initTheme } from "@/lib/theme";
import { AppShell } from "@/components/layout/AppShell";
import { CohortDashboard } from "@/components/dashboard/CohortDashboard";
import { SleeveDetailDrawer } from "@/components/dashboard/SleeveDetailDrawer";
import { PortfolioView } from "@/components/views/PortfolioView";
import { ResearchView } from "@/components/views/ResearchView";
import { OperationsView } from "@/components/views/OperationsView";
import { LoadingSkeleton } from "@/components/ui/LoadingSkeleton";

const REFRESH_MS = 30_000;
const REFRESH_SECONDS = REFRESH_MS / 1000;
const WS_URL = `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws/data`;

export type ViewKey = "cohort" | "portfolio" | "research" | "operations";

const VIEW_KEYS = new Set<ViewKey>(["cohort", "portfolio", "research", "operations"]);

function isViewKey(value: string | null): value is ViewKey {
  return value !== null && VIEW_KEYS.has(value as ViewKey);
}

/** Read the initial view, cohort, sleeve, and dev fixture out of the URL so refreshes and
 *  shared local links keep the operator where they were. */
function readParams(): {
  view: ViewKey | null;
  cohort: string | null;
  sleeve: string | null;
  fixture: string | null;
} {
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  const cohort = params.get("cohort");
  return {
    view: isViewKey(view) ? view : null,
    cohort: cohort || null,
    // The open sleeve is a *stable id*, never a display name: reopening this link must
    // land on the same sleeve even when another cohort has one with the same name.
    sleeve: params.get("sleeve") || null,
    fixture: params.get("fixture"),
  };
}

function writeParams(view: ViewKey, cohort: string | null, sleeve: string | null): void {
  const params = new URLSearchParams(window.location.search);
  params.set("view", view);
  if (cohort) params.set("cohort", cohort);
  else params.delete("cohort");
  if (sleeve) params.set("sleeve", sleeve);
  else params.delete("sleeve");
  const query = params.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
}

/** Dev-only: render a committed offline fixture instead of calling the server. */
const FIXTURE_NAME = import.meta.env.DEV ? readParams().fixture : null;

// Apply the stored light/dark preference before first paint.
initTheme();

export default function App() {
  const initial = useRef(readParams());
  const [requestedCohort, setRequestedCohort] = useState<string | null>(initial.current.cohort);
  const [openSleeveId, setOpenSleeveId] = useState<string | null>(initial.current.sleeve);
  const [view, setView] = useState<ViewKey | null>(initial.current.view);
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [socketStatus, setSocketStatus] = useState<ConnectionStatus>("connecting");
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    if (FIXTURE_NAME) {
      const { FIXTURES, FIXTURE_NAMES } = await import("@/lib/fixtures");
      const fixture = FIXTURES[FIXTURE_NAME];
      if (fixture) {
        setData(fixture);
        setError(null);
        setLastRefresh(new Date());
      } else {
        setError(`Unknown fixture '${FIXTURE_NAME}'. Available: ${FIXTURE_NAMES.join(", ")}`);
      }
      return;
    }
    try {
      const query = requestedCohort
        ? `?cohort=${encodeURIComponent(requestedCohort)}`
        : "";
      const resp = await fetch(`/api/data${query}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const newData = (await resp.json()) as DashboardData;
      setData(newData);
      setError(null);
      setLastRefresh(new Date());
    } catch (exc) {
      // Never substitute fixture data for a failed load: a dashboard that
      // reports account value, positions, and kill-switch state must surface
      // the outage rather than render plausible-looking stale demo numbers.
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [requestedCohort]);

  const wsUrl = requestedCohort
    ? `${WS_URL}?cohort=${encodeURIComponent(requestedCohort)}`
    : WS_URL;

  // WebSocket for real-time updates (server support optional; polling is the fallback)
  const handleWsMessage = useCallback((newData: DashboardData) => {
    setData(newData);
    setError(null);
    setLastRefresh(new Date());
  }, []);

  const handleStatusChange = useCallback(
    (status: ConnectionStatus) => {
      setSocketStatus(status);
      if (status === "fallback" && !pollingRef.current) {
        pollingRef.current = setInterval(() => void load(), REFRESH_MS);
      }
      if (status === "connected" && pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    },
    [load]
  );

  useWebSocket({
    url: wsUrl,
    onMessage: handleWsMessage,
    onStatusChange: handleStatusChange,
    reconnectInterval: 3000,
    maxReconnectAttempts: 5,
  });

  // Initial load and fallback polling
  useEffect(() => {
    void load();
    pollingRef.current = setInterval(() => void load(), REFRESH_MS);
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [load]);

  // In no-live mode with a real cohort, the cohort is the useful landing view.
  const defaultView: ViewKey =
    data && !data.live_enabled && data.cohort.available ? "cohort" : "portfolio";
  const activeView = view ?? defaultView;
  const selectedCohort = data?.cohort.selection.selected ?? requestedCohort;

  useEffect(() => {
    if (data) writeParams(activeView, selectedCohort, openSleeveId);
  }, [activeView, selectedCohort, openSleeveId, data]);

  const closeSleeve = useCallback(() => setOpenSleeveId(null), []);

  const connection: ConnectionState = data
    ? connectionState(socketStatus, lastRefresh !== null && error === null)
    : "reconnecting";

  const { historicalSleeves, historicalCurves } = useMemo(() => {
    const rows = (data?.sleeves ?? []).filter((row) => row.scope !== "official-cohort");
    const ids = new Set(rows.map((row) => row.sleeve_id));
    return {
      historicalSleeves: rows,
      historicalCurves: (data?.curves ?? []).filter((curve) => ids.has(curve.sleeve_id)),
    };
  }, [data]);

  if (!data) {
    return <LoadingSkeleton error={error} />;
  }

  return (
    <AppShell
      data={data}
      view={activeView}
      onSelectView={setView}
      connection={connection}
      lastRefresh={lastRefresh}
      error={error}
      refreshSeconds={REFRESH_SECONDS}
      onRefresh={load}
    >
      {activeView === "cohort" && (
        <CohortDashboard
          view={data.cohort}
          selectedCohort={selectedCohort}
          onSelectCohort={setRequestedCohort}
          onOpenSleeve={setOpenSleeveId}
        />
      )}

      {activeView === "portfolio" && (
        <PortfolioView data={data} />
      )}

      {activeView === "research" && (
        <ResearchView
          sleeves={historicalSleeves}
          curves={historicalCurves}
          validation={data.validation}
          regime={data.regime}
          benchmark={data.benchmark}
          onOpenSleeve={setOpenSleeveId}
        />
      )}

      {activeView === "operations" && (
        <OperationsView
          safety={data.safety}
          approvals={data.approvals}
          audit={data.audit}
          onKilled={load}
        />
      )}

      {/* Rendered outside the view switch so the record stays open while the operator
          changes cohort or view underneath it. Read-only; it has no action controls. */}
      <SleeveDetailDrawer
        sleeveId={openSleeveId}
        onClose={closeSleeve}
        useFixtures={FIXTURE_NAME !== null}
      />
    </AppShell>
  );
}
