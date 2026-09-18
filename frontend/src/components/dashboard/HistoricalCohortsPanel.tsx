import { Archive, ArrowRight, FileText } from 'lucide-react';
import type { HistoricalCohortView } from '@/lib/types';

interface Props {
  cohorts: HistoricalCohortView[];
  selectedCohort: string | null;
  onSelectCohort: (cohort: string) => void;
}

/**
 * Superseded cohorts, collapsed by default.
 *
 * They are never deleted and never hidden — a withdrawn experiment is still the record
 * of what happened — but they are one click away rather than in the operator's path,
 * and each one states plainly that it must not be run.
 */
export function HistoricalCohortsPanel({ cohorts, selectedCohort, onSelectCohort }: Props) {
  if (cohorts.length === 0) return null;

  return (
    <details className='group rounded-lg border border-border bg-card'>
      <summary className='flex cursor-pointer items-center gap-2 p-4 text-sm font-semibold text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-1 focus-visible:ring-primary/60'>
        <span className='transition-transform group-open:rotate-90'>›</span>
        <Archive className='h-4 w-4' />
        Historical Cohorts
        <span className='rounded-full bg-secondary px-2 py-0.5 text-xs font-medium text-muted-foreground'>
          {cohorts.length}
        </span>
      </summary>

      <div className='space-y-3 border-t border-border/60 p-4'>
        <p className='text-xs text-muted-foreground'>
          Retained unchanged for audit. These cohorts are not scheduled, not run, and not
          used for comparisons or benchmark resolution.
        </p>

        {cohorts.map((cohort) => {
          const isSelected = cohort.cohort_id === selectedCohort;
          return (
            <div
              key={cohort.cohort_id}
              className='rounded-md border border-border/70 bg-secondary/20 p-3'
            >
              <div className='flex flex-wrap items-center gap-2'>
                <span className='font-medium text-foreground'>{cohort.cohort_id}</span>
                <span className='rounded-full border border-warn/40 bg-warn/10 px-2 py-0.5 text-xs font-medium text-warn'>
                  {cohort.label}
                </span>
              </div>

              <p className='mt-1.5 text-sm text-muted-foreground'>{cohort.reason}</p>

              <div className='mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground'>
                {cohort.superseded_by !== null && (
                  <span className='inline-flex items-center gap-1'>
                    <ArrowRight className='h-3 w-3' />
                    Replaced by {cohort.superseded_by}
                  </span>
                )}
                {cohort.reference !== '' && (
                  <span className='inline-flex items-center gap-1'>
                    <FileText className='h-3 w-3' />
                    {cohort.reference}
                  </span>
                )}
              </div>

              <button
                type='button'
                onClick={() => onSelectCohort(cohort.cohort_id)}
                disabled={isSelected}
                className='mt-3 rounded-md border border-border bg-secondary px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary/70 disabled:cursor-default disabled:opacity-60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary/50'
              >
                {isSelected ? 'Currently shown' : 'View records'}
              </button>
            </div>
          );
        })}
      </div>
    </details>
  );
}
