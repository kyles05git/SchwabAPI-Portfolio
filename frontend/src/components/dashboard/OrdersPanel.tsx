import type { OrdersView } from "@/lib/types";
import { money, qty } from "@/lib/format";
import { PanelCard } from "./PanelCard";
import { DataTable } from "./DataTable";
import { ListOrdered } from "lucide-react";

interface Props {
  view: OrdersView;
}

export function OrdersPanel({ view }: Props) {
  return (
    <PanelCard
      title="Orders"
      subtitle="Live"
      icon={<ListOrdered className="h-3.5 w-3.5" />}
    >
      {!view.available ? (
        <p className="text-sm text-muted-foreground">{view.message ?? "Unavailable."}</p>
      ) : (
        <DataTable
          headers={["ID", "Status", "Side", "Symbol", "Qty", "Filled", "Limit"]}
          emptyMessage={`No orders in the last ${view.hours}h.`}
          rows={view.orders.map((o) => [
            <span className="font-mono text-xs text-muted-foreground">{o.order_id}</span>,
            o.working ? (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                <span className="h-1.5 w-1.5 rounded-full bg-primary animate-pulse-dot" />
                {o.status}
              </span>
            ) : (
              <span className="text-muted-foreground">{o.status}</span>
            ),
            <span className="text-foreground">{o.side ?? "—"}</span>,
            <span className="font-semibold text-foreground">{o.symbol ?? "—"}</span>,
            qty(o.quantity),
            qty(o.filled),
            money(o.limit_price),
          ])}
        />
      )}
    </PanelCard>
  );
}