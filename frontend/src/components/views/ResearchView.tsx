import type { SleeveRow, EquitySeries, ValidationView, RegimeView } from "@/lib/types";
import { PerformanceTimeline } from "../dashboard/PerformanceTimeline";
import { SleevesPanel } from "../dashboard/SleevesPanel";
import { ValidationPanel } from "../dashboard/ValidationPanel";
import { RegimePanel } from "../dashboard/RegimePanel";

interface Props {
  sleeves: SleeveRow[];
  curves: EquitySeries[];
  validation: ValidationView;
  regime: RegimeView;
  benchmark: string;
  /** Opens the read-only sleeve detail by stable id. */
  onOpenSleeve?: (sleeveId: string) => void;
}

export function ResearchView({
  sleeves,
  curves,
  validation,
  regime,
  benchmark,
  onOpenSleeve,
}: Props) {
  return (
    <section className="space-y-5" aria-label="Research">
      <PerformanceTimeline curves={curves} benchmark={benchmark} />
      <SleevesPanel rows={sleeves} curves={curves} onOpenSleeve={onOpenSleeve} />
      <div className="grid gap-5 lg:grid-cols-2">
        <ValidationPanel view={validation} />
        <RegimePanel view={regime} />
      </div>
    </section>
  );
}
