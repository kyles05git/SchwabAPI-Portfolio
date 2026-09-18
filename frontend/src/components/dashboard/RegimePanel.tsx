import type { RegimeView } from "@/lib/types";
import { n } from "@/lib/format";
import { PanelCard } from "./PanelCard";
import { Gauge } from "lucide-react";

interface Props {
  view: RegimeView;
}

export function RegimePanel({ view }: Props) {
  if (!view.available) {
    return (
      <PanelCard title="Market Regime" icon={<Gauge className="h-3.5 w-3.5" />}>
        <p className="text-sm text-muted-foreground">{view.message ?? "Unavailable."}</p>
      </PanelCard>
    );
  }

  const indicators = [
    { label: "SPY > 200DMA", ok: view.spy_above_200dma },
    { label: "50DMA > 200DMA", ok: view.trend_50_over_200 },
    { label: `Breadth > 50% (${Math.round(view.breadth_pct * 100)}%)`, ok: view.breadth_above_50 },
    { label: "Calm Volatility", ok: view.calm_volatility },
  ];

  return (
    <PanelCard title="Market Regime" icon={<Gauge className="h-3.5 w-3.5" />}>
      <div className="space-y-2.5">
        {/* Score */}
        <div className="flex items-center justify-between rounded-md border border-border bg-secondary/30 px-3 py-2.5">
          <span className="text-sm text-muted-foreground">Regime Score</span>
          <div className="flex items-center gap-3">
            <div className="h-1.5 w-20 overflow-hidden rounded-full bg-border">
              <div
                className="h-full rounded-full bg-primary transition-all duration-500"
                style={{ width: `${(view.score / 4) * 100}%` }}
              />
            </div>
            <span className="tnum text-sm font-bold text-foreground">{view.score}/4</span>
          </div>
        </div>

        {/* Exposure cap */}
        <div className="flex items-center justify-between rounded-md border border-border bg-secondary/30 px-3 py-2.5">
          <span className="text-sm text-muted-foreground">Gross-exposure cap</span>
          <span className="tnum text-sm font-semibold text-foreground">
            {Math.round((n(view.gross_exposure_cap) ?? 0) * 100)}%
          </span>
        </div>

        {/* Indicators */}
        {indicators.map(({ label, ok }) => (
          <div
            key={label}
            className="flex items-center justify-between rounded-md border border-border bg-secondary/30 px-3 py-2.5"
          >
            <span className="text-sm text-muted-foreground">{label}</span>
            <span
              className={`flex items-center gap-1.5 text-sm font-medium ${
                ok ? "text-profit" : "text-loss"
              }`}
            >
              <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-profit" : "bg-loss"}`} />
              {ok ? "On" : "Off"}
            </span>
          </div>
        ))}
      </div>
    </PanelCard>
  );
}