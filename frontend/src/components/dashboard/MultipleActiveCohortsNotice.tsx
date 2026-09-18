import { AlertTriangle, CalendarClock, HelpCircle } from 'lucide-react';
import type { CohortSelectionView } from '@/lib/types';
import { dateLabel } from './CohortStatus';

interface Props {
  selection: CohortSelectionView;
  onSelectCohort: (cohort: string) => void;
}

/**
 * More than one cohort is collecting at once.
 *
 * That is a legitimate transitional state — a challenger cohort starts before the
 * incumbent is retired — but it is never a resting state, because two live experiments
 * mean one of them is still owed a retire-or-keep decision. This banner exists so that
 * decision cannot be forgotten simply because the dashboard picked something sensible.
 *
 * Two shapes, one component:
 *
 * - **Resolved.** The newest cohort by persisted start session is on screen, the others
 *   are named with their start dates and are one click away.
 * - **Ambiguous.** Equal or missing start sessions mean there is no newest cohort. No
 *   cohort is shown at all, the reason is stated, and every candidate is offered
 *   explicitly. Defaulting to a guess here is the defect, not the fix.
 *
 * Nothing in this component changes a cohort's lifecycle. The only actions are "look at
 * this one instead"; retiring a cohort is a reviewed change made outside the dashboard.
 */
export function MultipleActiveCohortsNotice({ selection, onSelectCohort }: Props) {
  if (!selection.multiple_active && !selection.ambiguous) return null;

  const ambiguous = selection.ambiguous;
  const defaultCohort = selection.active_cohorts.find((item) => item.is_default) ?? null;
  const others = selection.active_cohorts.filter((item) => !item.is_default);

  return (
    <section
      role='status'
      aria-label='Multiple active cohorts'
      className={`rounded-lg border p-4 ${
        ambiguous ? 'border-loss/40 bg-loss/10' : 'border-warn/40 bg-warn/10'
      }`}
    >
      {/* Full class strings, not interpolated fragments: Tailwind scans source text. */}
      <div
        className={`flex items-center gap-2 text-sm font-semibold ${
          ambiguous ? 'text-loss' : 'text-warn'
        }`}
      >
        {ambiguous ? (
          <HelpCircle className='h-4 w-4 shrink-0' />
        ) : (
          <AlertTriangle className='h-4 w-4 shrink-0' />
        )}
        {ambiguous
          ? 'No cohort could be selected'
          : `${selection.active_cohorts.length} cohorts are collecting at once`}
      </div>

      <p className='mt-1.5 text-sm text-muted-foreground'>
        {ambiguous ? (
          selection.ambiguity_reason
        ) : (
          <>
            Showing the newest by start session
            {defaultCohort !== null && (
              <>
                {' '}
                (<span className='font-medium text-foreground'>{defaultCohort.cohort_id}</span>,
                started {dateLabel(defaultCohort.start_session)})
              </>
            )}
            . The {others.length === 1 ? 'cohort' : 'cohorts'} below{' '}
            {others.length === 1 ? 'is' : 'are'} still collecting and still{' '}
            {others.length === 1 ? 'needs' : 'need'} an explicit decision to retire or keep.
            Nothing here has been retired.
          </>
        )}
      </p>

      <ul className='mt-3 space-y-2'>
        {(ambiguous ? selection.active_cohorts : others).map((cohort) => (
          <li
            key={cohort.cohort_id}
            className='flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border/70 bg-card/60 px-3 py-2'
          >
            <span className='font-medium text-foreground'>{cohort.cohort_id}</span>
            <span className='inline-flex items-center gap-1 text-xs text-muted-foreground'>
              <CalendarClock className='h-3 w-3' />
              {cohort.start_session === null
                ? 'No persisted start session'
                : `Started ${dateLabel(cohort.start_session)}`}
            </span>
            <button
              type='button'
              onClick={() => onSelectCohort(cohort.cohort_id)}
              className='ml-auto rounded-md border border-border bg-secondary px-3 py-1 text-xs font-medium text-foreground transition-colors hover:bg-secondary/70 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary/50'
            >
              View records
            </button>
          </li>
        ))}
      </ul>

      <p className='mt-3 text-xs text-muted-foreground'>
        Retiring a cohort is a reviewed change to the lifecycle registry, never something
        this screen does on its own. See docs/operations/cohort-rollover.md.
      </p>
    </section>
  );
}
