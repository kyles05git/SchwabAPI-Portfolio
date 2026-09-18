import type { CohortDashboardView } from '@/lib/types';
import { CohortSummary } from './CohortSummary';
import { CohortComparisonPanel } from './CohortComparisonPanel';
import { CohortReadinessPanel, CohortRunHealthPanel } from './CohortOperationsPanel';
import { CohortGatePanel } from './CohortGatePanel';
import { CohortReviewPanel } from './CohortReviewPanel';
import { HistoricalCohortsPanel } from './HistoricalCohortsPanel';
import { MultipleActiveCohortsNotice } from './MultipleActiveCohortsNotice';
import { StrategyDefinitionsPanel } from './StrategyDefinitionsPanel';

interface Props {
  view: CohortDashboardView;
  selectedCohort: string | null;
  onSelectCohort: (cohort: string) => void;
  /** Opens the read-only sleeve detail by stable id. Works for an explicitly viewed
   *  superseded cohort too: its members stay inspectable, just never actionable. */
  onOpenSleeve?: (sleeveId: string) => void;
}

/**
 * The Paper Cohort view: a compact summary first, then evidence in decreasing order
 * of operator urgency. Everything below the summary is progressively disclosed.
 */
export function CohortDashboard({
  view,
  selectedCohort,
  onSelectCohort,
  onOpenSleeve,
}: Props) {
  const identity = view.identity;

  return (
    <section className='space-y-5' aria-label='Official paper cohort'>
      {/* Above the summary, because "which experiment am I looking at" has to be settled
          before any figure underneath it means anything. */}
      <MultipleActiveCohortsNotice
        selection={view.selection}
        onSelectCohort={onSelectCohort}
      />

      <CohortSummary
        view={view}
        selectedCohort={selectedCohort}
        onSelectCohort={onSelectCohort}
      />

      {view.available && identity !== null && (
        <>
          <div className='grid gap-5 xl:grid-cols-2'>
            <CohortRunHealthPanel runHealth={view.run_health} phase={view.phase} />
            <CohortReadinessPanel readiness={view.readiness} phase={view.phase} />
          </div>

          <CohortComparisonPanel
            comparison={view.comparison}
            benchmarkName={identity.benchmark_sleeve_name}
            benchmarkId={identity.benchmark_sleeve}
            phase={view.phase}
            onOpenSleeve={onOpenSleeve}
          />

          <CohortGatePanel gate={view.operational_gate} phase={view.phase} />

          {/* Directly under the gate: the accounting and operator-decision rules above
              are the two the gate cannot evaluate without these records, so the evidence
              belongs next to the requirement it satisfies. */}
          <CohortReviewPanel review={view.cohort_review} />

          <StrategyDefinitionsPanel
            definitions={view.sleeve_definitions}
            benchmarkId={identity.benchmark_sleeve}
          />
        </>
      )}

      {/* Last and collapsed: superseded cohorts stay auditable without competing with
          the collection that is actually running. Rendered even when no cohort resolved,
          because that is exactly when the operator needs the way back into the records. */}
      <HistoricalCohortsPanel
        cohorts={view.selection.historical}
        selectedCohort={view.selection.selected}
        onSelectCohort={onSelectCohort}
      />
    </section>
  );
}
