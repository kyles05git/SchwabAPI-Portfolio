import type { CohortRunView } from '@/lib/types';
import { dateLabel, humanize } from './CohortStatus';

interface CoverageRow {
  symbol: string;
  state: string;
  latestOfficialSession: string | null;
  evidenceSource: string | null;
  expectedIntervals: number | null;
  observedIntervals: number | null;
}

interface ProviderWait {
  targetSession: string;
  symbols: CoverageRow[];
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function text(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function count(value: unknown): number | null {
  return typeof value === 'number' ? value : null;
}

/** Error codes a non-terminal `awaiting-data` run can carry. The last row wins. */
const WAIT_CODES = ['awaiting_data', 'awaiting_reauthentication'];

/**
 * Whether this run is parked on the one wait that retrying cannot clear.
 *
 * The trail is ordered by when each distinct verdict was *last* reached, so the last
 * row carrying a wait code is the wait the run is in now. Mirrors
 * `sleeve_runs.awaits_reauthentication`.
 */
export function awaitsReauthentication(run: CohortRunView): boolean {
  if (run.status !== 'awaiting-data') return false;
  const current = [...run.errors].reverse().find((item) => WAIT_CODES.includes(item.code));
  return current?.code === 'awaiting_reauthentication';
}

function providerWait(run: CohortRunView): ProviderWait | null {
  // `errors` is append-only, and a new row is appended whenever the sanitized wait
  // context differs, so the *last* awaiting_data row is the current wait. Taking the
  // first would pin the panel to attempt 0's coverage for the whole wait. Matches
  // `sleeve_runs._expire_awaiting_data`, which reads `waiting[-1]` for the same reason.
  const error = [...run.errors].reverse().find((item) => item.code === 'awaiting_data');
  const marketData = record(error?.context?.market_data);
  const targetSession = text(marketData?.target_session);
  if (!marketData || !targetSession || !Array.isArray(marketData.symbols)) return null;

  const symbols = marketData.symbols.flatMap((value): CoverageRow[] => {
    const item = record(value);
    const symbol = text(item?.symbol);
    const state = text(item?.state);
    if (!item || !symbol || !state) return [];
    return [
      {
        symbol,
        state,
        latestOfficialSession: text(item.latest_official_session),
        evidenceSource: text(item.evidence_source),
        expectedIntervals: count(item.expected_interval_count),
        observedIntervals: count(item.observed_interval_count),
      },
    ];
  });
  return { targetSession, symbols };
}

function easternDateTime(value: string | null): string {
  if (!value) return 'Unavailable';
  const [date, time = ''] = value.split('T');
  return `${date} ${time.slice(0, 5)} ET`.trim();
}

export function ProviderDataStatus({ run }: { run: CohortRunView }) {
  if (run.status !== 'awaiting-data') return null;
  const wait = providerWait(run);
  // Same retryable state, opposite instruction. A wait on provider data clears itself
  // on the next scheduler tick; a wait on authentication only clears when a human logs
  // in, and the session is lost at the deadline if nobody does.
  const needsAuth = awaitsReauthentication(run);

  return (
    <div className='mt-3 rounded border border-warn/30 bg-warn/10 p-2 text-xs text-foreground'>
      <p className='font-medium'>
        {needsAuth
          ? 'Awaiting Schwab reauthentication — retryable'
          : 'Awaiting provider data — retryable'}
      </p>
      {needsAuth && (
        <p className='mt-1 text-muted-foreground'>
          No member executed and nothing was recorded. Run{' '}
          <code>python -m schwab_trader auth login</code> on the runner machine, then re-run
          this cohort before the deadline below.
        </p>
      )}
      <p className='mt-1 text-muted-foreground'>
        Target session {dateLabel(wait?.targetSession ?? run.scheduled_for)}
        {' / actual retry deadline '}
        {easternDateTime(run.retry_deadline_et ?? null)}
      </p>
      {wait && wait.symbols.length > 0 && (
        <ul className='mt-2 grid gap-1 sm:grid-cols-2'>
          {wait.symbols.map((item) => (
            <li key={item.symbol}>
              <span className='font-semibold'>{item.symbol}</span>
              {' / latest official '}
              {dateLabel(item.latestOfficialSession)}
              {' / '}
              {item.expectedIntervals === null
                ? 'official daily covers target'
                : `derived coverage ${item.observedIntervals ?? 0}/${item.expectedIntervals}`}
              {item.evidenceSource ? ` / ${humanize(item.evidenceSource)}` : ''}
              {item.state === 'provider_error' ? ' / provider error' : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
