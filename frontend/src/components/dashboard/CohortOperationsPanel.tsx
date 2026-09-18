import { Activity, DatabaseZap } from 'lucide-react';
import type { CohortPhaseView, CohortRunHealthView, ReadinessEvidenceView } from '@/lib/types';
import { PanelCard } from './PanelCard';
import {
  EmptyState,
  ShortId,
  StatusBadge,
  TechnicalDetails,
  dateLabel,
  dateTimeLabel,
  humanize,
  ratioPct,
  runTimingLabel,
} from './CohortStatus';
import { ProviderDataStatus, awaitsReauthentication } from './ProviderDataStatus';

/** Run states worth opening automatically, because they need an operator. */
const NEEDS_REVIEW = new Set(['partial', 'interrupted', 'failed', 'missed']);

/**
 * Data readiness for each cohort member.
 *
 * Before the first official run there is no readiness evidence at all, so this renders
 * a single sentence rather than one identical "unavailable" card per sleeve.
 */
export function CohortReadinessPanel({
  readiness,
  phase,
}: {
  readiness: ReadinessEvidenceView[];
  phase: CohortPhaseView | null;
}) {
  const hasEvidence = readiness.some((item) => item.evidence_status !== 'unavailable');

  return (
    <PanelCard title='Data readiness' icon={<DatabaseZap className='h-3.5 w-3.5' />}>
      {readiness.length === 0 || !hasEvidence ? (
        <EmptyState>
          Readiness evidence will appear after the first official cohort run
          {phase?.next_scheduled_session
            ? `, scheduled for ${dateLabel(phase.next_scheduled_session)}.`
            : '.'}
        </EmptyState>
      ) : (
        <div className='overflow-x-auto'>
          <table className='w-full min-w-[36rem] border-collapse text-sm'>
            <thead>
              <tr className='border-b border-border/60 text-left text-xs font-medium text-muted-foreground'>
                <th scope='col' className='py-2 pr-3 font-medium'>
                  Sleeve
                </th>
                <th scope='col' className='py-2 pr-3 font-medium'>
                  Ready
                </th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>
                  Coverage
                </th>
                <th scope='col' className='py-2 pr-3 font-medium'>
                  Latest session
                </th>
                <th scope='col' className='py-2 font-medium'>
                  Reason
                </th>
              </tr>
            </thead>
            <tbody>
              {readiness.map((item) => (
                <tr key={item.sleeve_id} className='border-b border-border/40 last:border-0'>
                  <td className='py-2 pr-3 font-medium text-foreground'>{item.sleeve_name}</td>
                  <td className='py-2 pr-3'>
                    <StatusBadge status={item.evidence_status} />
                  </td>
                  <td className='py-2 pr-3 text-right tnum text-foreground'>
                    {item.quote_coverage === null ? '—' : ratioPct(item.quote_coverage)}
                  </td>
                  <td className='py-2 pr-3 tnum text-muted-foreground'>
                    {dateLabel(item.session_date)}
                  </td>
                  <td className='py-2 text-muted-foreground'>
                    {item.reason_codes.length > 0
                      ? item.reason_codes.map(humanize).join(', ')
                      : item.ready === true
                        ? 'All checks passed'
                        : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {hasEvidence && (
        <TechnicalDetails className='mt-3' label='Snapshot identifiers and reason codes'>
          <dl className='space-y-3 text-xs'>
            {readiness.map((item) => (
              <div key={item.sleeve_id}>
                <dt className='flex flex-wrap items-center gap-2 font-medium text-foreground'>
                  {item.sleeve_name}
                  <ShortId value={item.sleeve_id} />
                </dt>
                <dd className='mt-1 space-y-0.5 text-muted-foreground'>
                  {Object.entries(item.snapshot_ids).length === 0 ? (
                    <p>No snapshot identifiers recorded.</p>
                  ) : (
                    Object.entries(item.snapshot_ids).map(([kind, id]) => (
                      <p key={kind} className='flex flex-wrap items-center gap-2'>
                        <span>{humanize(kind)}</span>
                        <ShortId value={id} />
                      </p>
                    ))
                  )}
                  {item.reason_codes.length > 0 && (
                    <p className='font-mono text-[11px]'>{item.reason_codes.join(', ')}</p>
                  )}
                </dd>
              </div>
            ))}
          </dl>
        </TechnicalDetails>
      )}
    </PanelCard>
  );
}

/**
 * Scheduled-run health: the latest due run first, older runs collapsed.
 *
 * Only runs that actually need an operator are expanded automatically.
 */
export function CohortRunHealthPanel({
  runHealth,
  phase,
}: {
  runHealth: CohortRunHealthView;
  phase: CohortPhaseView | null;
}) {
  const latest = phase?.latest_due_run ?? null;
  const latestRun = runHealth.runs.find((run) => run.run_id === latest?.run_id) ?? null;
  const latestTiming = latestRun?.timing ?? null;
  // Runs that are pre-close, inside grace, or awaiting provider evidence through the
  // actual deadline get their own block so a retryable run never reads as failed.
  const outstanding = runHealth.runs.filter(
    (run) =>
      run.run_id !== latest?.run_id &&
      (run.timing === 'upcoming' ||
        run.timing === 'awaiting-execution' ||
        run.timing === 'awaiting-provider-data' ||
        run.timing === 'overdue')
  );
  const outstandingIds = new Set(outstanding.map((run) => run.run_id));
  const history = runHealth.runs.filter(
    (run) => run.run_id !== latest?.run_id && !outstandingIds.has(run.run_id)
  );

  return (
    <PanelCard title='Run health' icon={<Activity className='h-3.5 w-3.5' />}>
      {!runHealth.available ? (
        <EmptyState tone='pending'>
          {runHealth.message ?? 'Durable cohort-run evidence is unavailable.'}
        </EmptyState>
      ) : latest === null ? (
        <EmptyState>
          {phase?.awaiting_execution_session
            ? `The ${dateLabel(
                phase.awaiting_execution_session
              )} session has closed and its run has not started yet.`
            : phase?.next_scheduled_session
              ? `No session has come due yet. The next is scheduled for ${dateLabel(
                  phase.next_scheduled_session
                )}.`
              : 'No durable official runs have been recorded.'}
        </EmptyState>
      ) : (
        <div
          className={`rounded-md border p-3 ${
            latestTiming === 'overdue'
              ? 'border-loss/30 bg-loss/10'
              : latestTiming === 'awaiting-provider-data'
                ? 'border-warn/30 bg-warn/10'
              : 'border-border/60 bg-secondary/15'
          }`}
        >
          <div className='flex flex-wrap items-center justify-between gap-2'>
            <div className='flex flex-wrap items-center gap-2'>
              <StatusBadge
                status={latest.status}
                label={
                  latest.status === 'awaiting-data'
                    ? latestRun !== null && awaitsReauthentication(latestRun)
                      ? 'Awaiting Schwab reauthentication — retryable'
                      : 'Awaiting provider data — retryable'
                    : undefined
                }
              />
              {latestTiming && latestTiming !== 'executed' && (
                <StatusBadge status={latestTiming} label={runTimingLabel(latestTiming)} />
              )}
              <span className='text-sm font-semibold text-foreground'>
                {dateLabel(latest.scheduled_for)}
              </span>
              <span className='text-xs text-muted-foreground'>Latest due run</span>
            </div>
            <span className='tnum text-sm text-foreground'>
              {latest.completed_members} / {latest.expected_members} members
            </span>
          </div>
          {latest.error_summary && (
            <p
              className={`mt-2 text-sm ${
                latestTiming === 'awaiting-provider-data' ? 'text-warn' : 'text-loss'
              }`}
            >
              {latest.error_summary}
            </p>
          )}
          {latestRun && <ProviderDataStatus run={latestRun} />}
        </div>
      )}

      {runHealth.available && outstanding.length > 0 && (
        <ul className='mt-3 space-y-2'>
          {outstanding.map((run) => (
            <li
              key={run.run_id}
              className={`flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2 ${
                run.timing === 'overdue'
                  ? 'border-loss/30 bg-loss/10'
                  : run.timing === 'awaiting-provider-data'
                    ? 'border-warn/30 bg-warn/10'
                  : 'border-border/60 bg-secondary/15'
              }`}
            >
              <span className='flex flex-wrap items-center gap-2'>
                <StatusBadge status={run.timing} label={runTimingLabel(run.timing)} />
                <span className='text-sm font-medium text-foreground'>
                  {dateLabel(run.scheduled_for)}
                </span>
              </span>
              <span className='text-xs text-muted-foreground'>
                {run.timing === 'upcoming'
                  ? 'Scheduled; the session has not closed yet'
                  : run.timing === 'awaiting-execution'
                    ? 'Session closed; the run has not started yet'
                    : run.timing === 'awaiting-provider-data'
                      ? `Provider evidence incomplete; retryable until ${dateTimeLabel(
                          run.retry_deadline_et ?? null
                        )}`
                    : 'Past the execution grace period with no result'}
              </span>
            </li>
          ))}
        </ul>
      )}

      {history.length > 0 && (
        <TechnicalDetails className='mt-3' label={`Run history (${history.length})`}>
          <div className='space-y-2'>
            {history.slice(0, 20).map((run) => (
              <details
                key={run.run_id}
                open={NEEDS_REVIEW.has(run.status)}
                className='rounded-md border border-border/50 bg-background/20 px-3 py-2'
              >
                <summary className='flex cursor-pointer list-none flex-wrap items-center justify-between gap-2'>
                  <span className='flex flex-wrap items-center gap-2'>
                    <StatusBadge status={run.status} />
                    <span className='text-sm font-medium text-foreground'>
                      {dateLabel(run.scheduled_for)}
                    </span>
                  </span>
                  <span className='tnum text-xs text-muted-foreground'>
                    {run.completed_members.length} / {run.expected_members.length} members
                  </span>
                </summary>
                <div className='mt-3 border-t border-border/40 pt-3 text-xs'>
                  <p className='text-muted-foreground'>
                    Started {dateTimeLabel(run.started_at)} · Completed{' '}
                    {dateTimeLabel(run.completed_at)}
                  </p>
                  {run.errors.length > 0 && (
                    <ul className='mt-2 space-y-1 rounded border border-loss/30 bg-loss/10 p-2 text-loss'>
                      {run.errors.map((error, index) => (
                        <li key={`${error.code}-${error.member_id ?? index}`}>
                          {humanize(error.code)}: {error.message}
                        </li>
                      ))}
                    </ul>
                  )}
                  <ul className='mt-2 space-y-1'>
                    {run.members.map((member) => (
                      <li
                        key={member.sleeve_id}
                        className='flex flex-wrap items-center justify-between gap-2'
                      >
                        <span className='text-foreground'>{member.sleeve_name}</span>
                        <span className='flex items-center gap-2'>
                          {member.error && (
                            <span className='text-loss'>{member.error.message}</span>
                          )}
                          <StatusBadge status={member.status} />
                        </span>
                      </li>
                    ))}
                  </ul>
                  <dl className='mt-2 flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground'>
                    <span className='flex items-center gap-1.5'>
                      <dt>Cohort snapshot</dt>
                      <dd>
                        <ShortId value={run.snapshot_id} />
                      </dd>
                    </span>
                    <span className='flex items-center gap-1.5'>
                      <dt>Quote snapshot</dt>
                      <dd>
                        <ShortId value={run.quote_snapshot_id} />
                      </dd>
                    </span>
                  </dl>
                </div>
              </details>
            ))}
          </div>
        </TechnicalDetails>
      )}
    </PanelCard>
  );
}
