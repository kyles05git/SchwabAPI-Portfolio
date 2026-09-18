import type { SafetyView } from "@/lib/types";
import { n, money } from "@/lib/format";
import type { Num } from "@/lib/types";
import { PanelCard } from "./PanelCard";
import { Shield } from "lucide-react";

interface Props {
  view: SafetyView;
}

/**
 * Autonomous-safety limits and today's activity.
 *
 * The kill-switch *control* lives in {@link KillSwitchBar}, which is pinned below the
 * header on every view so it is reachable without navigating here.
 */
export function SafetyPanel({ view }: Props) {
  const limitDisplay = (value: Num) => {
    const parsed = n(value);
    return parsed ? (
      <span className="font-medium text-foreground">{money(value)}</span>
    ) : (
      <span className="text-muted-foreground">off</span>
    );
  };

  const rows: [string, React.ReactNode][] = [
    [
      "Kill switch",
      view.kill_engaged ? (
        <span className="font-semibold text-loss">
          ENGAGED{view.kill_since ? ` (${new Date(view.kill_since).toLocaleString()})` : ""}
        </span>
      ) : (
        <span className="font-medium text-profit">Clear</span>
      ),
    ],
    ["Reason", view.kill_reason ?? <span className="text-muted-foreground">—</span>],
    ["Trades today", <span className="font-medium text-foreground">{view.trades_today}</span>],
    ["Realized P&L today", <span className="font-medium text-foreground">{money(view.realized_pnl)}</span>],
    [
      "Opening equity",
      view.start_equity !== null ? (
        <span className="font-medium text-foreground">{money(view.start_equity)}</span>
      ) : (
        <span className="text-muted-foreground">—</span>
      ),
    ],
    ["Capital cap", limitDisplay(view.capital_cap)],
    ["Daily loss limit", limitDisplay(view.daily_loss_limit)],
    [
      "Max trades/day",
      view.max_trades_per_day ? (
        <span className="font-medium text-foreground">{view.max_trades_per_day}</span>
      ) : (
        <span className="text-muted-foreground">off</span>
      ),
    ],
    ["Max order notional", <span className="font-medium text-foreground">{money(view.max_order_notional)}</span>],
  ];

  return (
    <PanelCard title="Autonomous Safety" icon={<Shield className="h-3.5 w-3.5" />}>
      <div className="space-y-2">
        {rows.map(([label, value]) => (
          <div
            key={label}
            className="flex items-center justify-between rounded-md border border-border bg-secondary/30 px-3 py-2"
          >
            <span className="text-sm text-muted-foreground">{label}</span>
            <span className="tnum text-sm text-right">{value}</span>
          </div>
        ))}

        <p className="mt-3 text-xs text-muted-foreground">
          {view.kill_engaged ? (
            <>
              Halted. Resume on the CLI:{" "}
              <code className="rounded bg-background px-1.5 py-0.5 font-mono text-xs text-primary">
                schwab-trader safety resume
              </code>
            </>
          ) : (
            "The kill switch is available at the top of every view."
          )}
        </p>
      </div>
    </PanelCard>
  );
}