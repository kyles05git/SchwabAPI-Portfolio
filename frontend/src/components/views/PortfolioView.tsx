import type { DashboardData } from "@/lib/types";
import { HeroStats } from "../dashboard/HeroStats";
import { PositionsPanel } from "../dashboard/PositionsPanel";
import { OrdersPanel } from "../dashboard/OrdersPanel";
import { ReconciliationPanel } from "../dashboard/ReconciliationPanel";
import { TaxLotsPanel } from "../dashboard/TaxLotsPanel";
import { WifiOff } from "lucide-react";

interface Props {
  data: DashboardData;
}

export function PortfolioView({ data }: Props) {
  if (!data.live_enabled) {
    return (
      <section className="space-y-5" aria-label="Portfolio">
        <div className="rounded-lg border border-border bg-card p-8 text-center">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-muted mb-4">
            <WifiOff className="h-5 w-5 text-muted-foreground" />
          </div>
          <h2 className="text-lg font-semibold text-foreground">Broker access disabled</h2>
          <p className="mt-2 text-sm text-muted-foreground max-w-md mx-auto">
            This server was started with <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">--no-live</code>,
            so account balances, positions, and orders are unavailable rather than zero.
            Paper cohort and local research data may still be available.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="space-y-5" aria-label="Portfolio">
      <HeroStats summary={data.summary} />

      <div className="grid gap-5 lg:grid-cols-2">
        <PositionsPanel view={data.positions} />
        <OrdersPanel view={data.orders} />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <ReconciliationPanel view={data.reconciliation} />
        <TaxLotsPanel view={data.tax_lots} />
      </div>
    </section>
  );
}
