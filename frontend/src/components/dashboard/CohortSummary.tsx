import { AlertTriangle, FlaskConical } from 'lucide-react';
import type { CohortDashboardView } from '@/lib/types';
import { money } from '@/lib/format';
import {
  EmptyState,
  Metric,
  PhaseBadge,
  ShortId,
  StatusBadge,
  TechnicalDetails,
  TimingBadge,
  dateLabel,
  dateTimeLabel,
  longDateLabel,
  ratioPct,
} from './CohortStatus';

interface Props {
  view: CohortDashboardView;
  selectedCohort: string | null;
  onSelectCohort: (cohort: string) => void;
}

function ProgressBar({ ratio, target, done }: { ratio: number; target: number; done: number }) {
  const pct = Math.round(Math.min(1, Math.max(0, ratio)) * 100);
  return (
    <div
      role='progressbar'
      aria-valuemin={0}
      aria-valuemax={target}
      aria-valuenow={done}
      aria-label={`${done} of ${target} review sessions complete`}
      className='h-2 w-full overflow-hidden rounded-full bg-secondary'
    >
      <div
        className={`h-full rounded-full ${pct === 0 ? '' : 'bg-primary'}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

/**
 * The first cohort screen. Answers, without scrolling: what is running, has it
 * started, how far is it from the 30-session review, did the latest due run finish,
 * is data ready, and what should the operator do next.
 */
export function CohortSummary({ view, selectedCohort, onSelectCohort }: Props) {
  const { identity, phase } = view;
  const selection = view.selection;
  // The picker offers the cohorts still collecting. A superseded one is reached from the
  // Historical Cohorts section, and appears here only while it is the cohort on screen,
  // so the control always reflects what is being shown without inviting a switch to it.
  const options =
    selection.selected !== null && !selection.active.includes(selection.selected)
      ? [...selection.active, selection.selected]
      : selection.active;
  // A picker with one option is noise; show it only when selection is meaningful.
  const showSelector = options.length > 1;
  const historical =
    selection.historical.find((item) => item.cohort_id === selection.selected) ?? null;

  if (!view.available || identity === null || phase === null) {
    return (
      <section className='rounded-lg border border-border bg-card p-5' aria-label='Official paper cohort'>
        <div className='flex items-center gap-2 text-muted-foreground'>
          <FlaskConical className='h-4 w-4' />
          <h2 className='text-sm font-semibold'>Official paper cohort</h2>
        </div>
        <div className='mt-3'>
          {/* When the default was ambiguous, the notice above already carries the full
              reason and the way out of it. Repeating it verbatim here reads as two
              different problems. */}
          <EmptyState tone='pending'>
            {selection.ambiguous
              ? 'No cohort is shown because more than one is active and the records cannot say which is newest. See the notice above, or select one explicitly.'
              : (view.message ?? 'No official paper cohort is available.')}
          </EmptyState>
        </div>
      </section>
    );
  }

  const latest = phase.latest_due_run;
  const readinessReady = view.readiness.filter((item) => item.ready === true).length;
  const readinessKnown = view.readiness.filter((item) => item.ready !== null).length;
  // A session that has not closed, or that closed inside the scheduler's grace period,
  // owes nothing yet. Say that instead of reporting an absence as a shortfall.
  const awaitingSession = phase.timing_state === 'awaiting-execution';
  const outstandingSession = awaitingSession || phase.timing_state === 'upcoming';
  const nextOwedSession = phase.awaiting_execution_session ?? phase.next_scheduled_session;

  return (
    <section
      className='rounded-lg border border-border bg-card'
      aria-label={`Official paper cohort ${identity.cohort_id}`}
    >
      {/* Every figure below this line belongs to a closed record, so say so before any
          of them are read, not in a footnote underneath them. */}
      {historical !== null && (
        <div className='rounded-t-lg border-b border-warn/30 bg-warn/10 p-4' role='status'>
          <div className='flex items-center gap-2 text-sm font-semibold text-warn'>
            <AlertTriangle className='h-4 w-4 shrink-0' />
            {historical.label}
          </div>
          <p className='mt-1 text-sm text-muted-foreground'>
            {historical.reason}
            {historical.superseded_by !== null && (
              <>
                {' '}
                The active cohort is{' '}
                <button
                  type='button'
                  onClick={() => onSelectCohort(historical.superseded_by as string)}
                  className='font-medium text-foreground underline underline-offset-2 hover:text-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary/50'
                >
                  {historical.superseded_by}
                </button>
                .
              </>
            )}
          </p>
        </div>
      )}

      <div className='flex flex-col gap-3 border-b border-border/60 p-5 sm:flex-row sm:items-start sm:justify-between'>
        <div className='min-w-0'>
          <div className='flex flex-wrap items-center gap-2'>
            <FlaskConical className='h-4 w-4 shrink-0 text-primary' />
            <h2 className='truncate text-lg font-semibold text-foreground'>
              {identity.cohort_id}
            </h2>
            <PhaseBadge phase={phase.phase} />
            {phase.timing_state !== 'no-session-scheduled' && (
              <TimingBadge timing={phase.timing_state} />
            )}
          </div>
          <p className='mt-1.5 text-sm text-muted-foreground'>
            {phase.headline}
            {identity.starting_capital_per_sleeve !== null && (
              <> · {money(identity.starting_capital_per_sleeve)} per sleeve</>
            )}
          </p>
        </div>

        {showSelector && (
          <label className='text-xs font-medium text-muted-foreground'>
            Cohort
            <select
              value={selectedCohort ?? ''}
              onChange={(event) => onSelectCohort(event.target.value)}
              className='mt-1 block w-full min-w-56 rounded-md border border-border bg-secondary px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary/50'
            >
              {options.map((cohort) => (
                <option key={cohort} value={cohort}>
                  {cohort}
                  {cohort === historical?.cohort_id ? ' (historical)' : ''}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div className='grid gap-5 p-5 sm:grid-cols-2 lg:grid-cols-4'>
        <Metric
          label={`Review sessions`}
          value={`${phase.completed_due_sessions} of ${phase.review_target}`}
          emphasis
          detail={
            phase.sessions_remaining > 0
              ? `${phase.sessions_remaining} more before the review`
              : 'Review target reached'
          }
        />
        <Metric
          label='Start date'
          value={longDateLabel(phase.start_session)}
          detail={`${identity.member_names.length} members`}
        />
        <Metric
          label='Benchmark'
          value={identity.benchmark_sleeve_name ?? 'Unavailable'}
          detail='Benchmark and control sleeve'
        />
        <Metric
          label='Latest observation'
          value={
            phase.latest_observation_date
              ? dateLabel(phase.latest_observation_date)
              : 'No official observations yet'
          }
          detail={
            phase.official_observations > 0
              ? `${phase.official_observations} official observations`
              : undefined
          }
        />
      </div>

      <div className='px-5'>
        <ProgressBar
          ratio={phase.progress_ratio}
          target={phase.review_target}
          done={phase.completed_due_sessions}
        />
      </div>

      <div className='grid gap-5 p-5 sm:grid-cols-2 lg:grid-cols-3'>
        <Metric
          label='Latest due run'
          value={
            latest ? (
              <span className='inline-flex items-center gap-2'>
                <StatusBadge status={latest.status} />
                <span className='text-sm'>{dateLabel(latest.scheduled_for)}</span>
              </span>
            ) : awaitingSession ? (
              <span className='text-sm font-normal text-warn'>Awaiting execution</span>
            ) : (
              <span className='text-sm font-normal text-muted-foreground'>
                No session has come due
              </span>
            )
          }
          detail={
            latest
              ? `${latest.completed_members} of ${latest.expected_members} members completed`
              : awaitingSession && nextOwedSession
                ? `${dateLabel(nextOwedSession)} closed; the run has not started`
                : nextOwedSession
                  ? `Next session ${dateLabel(nextOwedSession)}`
                  : undefined
          }
        />
        <Metric
          label='Completion reliability'
          value={
            phase.completion_reliability === null
              ? outstandingSession
                ? 'Awaiting evidence'
                : 'Not evaluated'
              : ratioPct(phase.completion_reliability)
          }
          detail={
            phase.completion_reliability === null
              ? awaitingSession
                ? "Measured once today's run records its result"
                : nextOwedSession
                  ? `Measured after the ${dateLabel(nextOwedSession)} close`
                  : 'No due sessions yet'
              : `${phase.completed_due_sessions} of ${phase.due_sessions} due sessions complete`
          }
        />
        <Metric
          label='Data readiness'
          value={
            readinessKnown === 0
              ? outstandingSession
                ? 'Awaiting evidence'
                : 'Awaiting first run'
              : `${readinessReady} of ${readinessKnown} ready`
          }
          detail={
            readinessKnown === 0
              ? 'Evidence appears after the first official run'
              : undefined
          }
        />
      </div>

      {phase.integrity_alerts.length > 0 && (
        <div className='mx-5 mb-5 rounded-md border border-loss/30 bg-loss/10 p-3'>
          <div className='flex items-center gap-2 text-sm font-semibold text-loss'>
            <AlertTriangle className='h-4 w-4' />
            {phase.integrity_alerts.length === 1
              ? '1 issue needs attention'
              : `${phase.integrity_alerts.length} issues need attention`}
          </div>
          <ul className='mt-2 list-disc space-y-1 pl-5 text-sm text-loss/90'>
            {phase.integrity_alerts.map((alert) => (
              <li key={alert}>{alert}</li>
            ))}
          </ul>
        </div>
      )}

      <div className='border-t border-border/60 bg-secondary/20 px-5 py-4'>
        <p className='text-xs font-medium text-muted-foreground'>Next action</p>
        <p className='mt-1 text-sm font-medium text-foreground'>{phase.next_action}</p>
        <TechnicalDetails className='mt-3' label='Technical details'>
          <dl className='grid gap-x-4 gap-y-1.5 text-xs sm:grid-cols-[11rem_1fr]'>
            <dt className='text-muted-foreground'>Contract version</dt>
            <dd className='text-foreground'>{view.contract_version}</dd>
            <dt className='text-muted-foreground'>Assessed as of</dt>
            <dd className='text-foreground'>
              {phase.now_et ? `${dateTimeLabel(phase.now_et)} ET` : dateLabel(phase.as_of)}
            </dd>
            <dt className='text-muted-foreground'>Latest closed session</dt>
            <dd className='text-foreground'>{dateLabel(phase.evidence_cutoff)}</dd>
            <dt className='text-muted-foreground'>Session timing</dt>
            <dd className='text-foreground'>
              {phase.timing_state}
              {phase.overdue_sessions.length > 0 &&
                ` · ${phase.overdue_sessions.length} overdue`}
            </dd>
            <dt className='text-muted-foreground'>Scheduled sessions</dt>
            <dd className='text-foreground'>
              {phase.due_sessions} due / {phase.total_scheduled_sessions} total
            </dd>
            <dt className='text-muted-foreground'>Benchmark sleeve id</dt>
            <dd>
              <ShortId value={identity.benchmark_sleeve} />
            </dd>
            <dt className='text-muted-foreground'>Cohort created</dt>
            <dd className='text-foreground'>{identity.created_at}</dd>
          </dl>
        </TechnicalDetails>
      </div>
    </section>
  );
}
