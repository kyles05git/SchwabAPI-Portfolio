import type { ReconciliationView } from "@/lib/types";
import { PanelCard } from "./PanelCard";
import { ListChecks } from "lucide-react";

interface Props {
  view: ReconciliationView;
}

export function ReconciliationPanel({ view }: Props) {
  if (view.completed_at === null) {
    return (
      <PanelCard title="Reconciliation Health" icon={<ListChecks className="h-3.5 w-3.5" />}>
        <p className="py-4 text-center text-sm text-muted-foreground">
          Never run. Start with{" "}
          <code className="rounded bg-background px-1.5 py-0.5 font-mono text-[11px] text-primary">
            schwab-trader reconcile
          </code>
          .
        </p>
      </PanelCard>
    );
  }

  const rows: Array<[string, React.ReactNode]> = [
    ["Last run", new Date(view.completed_at).toLocaleString()],
    [
      "Result",
      view.success ? (
        <span className="font-semibold text-profit">Complete</span>
      ) : (
        <span className="font-semibold text-loss">Failed</span>
      ),
    ],
    ["Orders seen", String(view.orders_seen)],
    ["Lifecycle transitions", String(view.transitions)],
    ["New fills applied", String(view.fills_applied)],
    [
      "Discrepancies",
      <span className={view.discrepancies ? "font-semibold text-warn" : "text-profit"}>
        {String(view.discrepancies)}
      </span>,
    ],
    [
      "Uncertain fill writes",
      <span className={view.pending_fill_applications ? "font-semibold text-loss" : "text-profit"}>
        {String(view.pending_fill_applications)}
      </span>,
    ],
  ];

  return (
    <PanelCard title="Reconciliation Health" icon={<ListChecks className="h-3.5 w-3.5" />}>
      <div className="space-y-2">
        {rows.map(([label, value]) => (
          <div
            key={label}
            className="flex items-center justify-between rounded-md border border-border bg-secondary/30 px-3 py-2"
          >
            <span className="text-sm text-muted-foreground">{label}</span>
            <span className="tnum text-right text-sm text-foreground">{value}</span>
          </div>
        ))}
      </div>
    </PanelCard>
  );
}
