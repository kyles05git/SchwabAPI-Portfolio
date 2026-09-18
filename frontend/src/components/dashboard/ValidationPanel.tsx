import type { ValidationView } from "@/lib/types";
import { pct, signClass } from "@/lib/format";
import { PanelCard } from "./PanelCard";
import { DataTable } from "./DataTable";
import { CheckCircle2 } from "lucide-react";

interface Props {
  view: ValidationView;
}

export function ValidationPanel({ view }: Props) {
  return (
    <PanelCard
      title="Strategy Validation"
      icon={<CheckCircle2 className="h-3.5 w-3.5" />}
    >
      <DataTable
        headers={["Strategy", "Universe", "Validated", "Pass / Min", "Excess"]}
        emptyMessage="No validation verdicts yet. Run 'validate run'."
        rows={view.rows.map((r) => [
          <span className="font-semibold text-foreground">{r.strategy}</span>,
          <span className="text-muted-foreground">{r.universe}</span>,
          r.validated ? (
            <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-profit">
              <span className="h-1.5 w-1.5 rounded-full bg-profit" />
              VALIDATED
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 text-xs text-loss">
              <span className="h-1.5 w-1.5 rounded-full bg-loss" />
              No
            </span>
          ),
          `${Math.round(r.pass_rate * 100)}% / ${Math.round(r.min_pass_rate * 100)}%`,
          r.mean_excess_pct === null ? (
            <span className="text-muted-foreground">—</span>
          ) : (
            <span className={signClass(r.mean_excess_pct)}>{pct(r.mean_excess_pct, true)}</span>
          ),
        ])}
      />
    </PanelCard>
  );
}