import type { TaxLotsView } from "@/lib/types";
import { money, qty } from "@/lib/format";
import { PanelCard } from "./PanelCard";
import { DataTable } from "./DataTable";
import { Receipt } from "lucide-react";

interface Props {
  view: TaxLotsView;
}

export function TaxLotsPanel({ view }: Props) {
  return (
    <PanelCard title="Tax Lots" icon={<Receipt className="h-3.5 w-3.5" />}>
      <DataTable
        headers={["Symbol", "Qty", "Cost/sh", "Acquired", "Days", "Term"]}
        emptyMessage="No tax lots recorded (they accrue from fills)."
        rows={view.rows.map((r) => [
          <span className="font-semibold text-foreground">{r.symbol}</span>,
          qty(r.quantity),
          money(r.cost_per_share),
          <span className="text-muted-foreground">{r.acquired}</span>,
          String(r.days_held),
          r.long_term ? (
            <span className="rounded-full bg-profit/10 px-2 py-0.5 text-[11px] font-medium text-profit">
              Long
            </span>
          ) : (
            <span className="rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
              Short
            </span>
          ),
        ])}
      />
    </PanelCard>
  );
}