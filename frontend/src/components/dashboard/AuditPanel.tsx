import type { AuditView } from "@/lib/types";
import { PanelCard } from "./PanelCard";
import { DataTable } from "./DataTable";
import { ScrollText } from "lucide-react";

interface Props {
  view: AuditView;
}

export function AuditPanel({ view }: Props) {
  return (
    <PanelCard title="Recent Activity" subtitle="Audit" icon={<ScrollText className="h-3.5 w-3.5" />}>
      <DataTable
        headers={["Time", "Command", "Event", "Detail"]}
        emptyMessage="No audit entries yet."
        rows={view.rows.map((r) => [
          <span className="font-mono text-xs text-muted-foreground">
            {new Date(r.ts).toLocaleString(undefined, {
              month: "2-digit",
              day: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
            })}
          </span>,
          <span className="font-medium text-foreground">{r.command}</span>,
          <span className="rounded-full bg-secondary px-2 py-0.5 text-[11px] text-muted-foreground">
            {r.event}
          </span>,
          <span className="text-muted-foreground">{r.detail ?? ""}</span>,
        ])}
      />
    </PanelCard>
  );
}