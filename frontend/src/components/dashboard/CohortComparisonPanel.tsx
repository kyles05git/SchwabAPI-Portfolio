import { Fragment, useMemo, useState } from 'react';
import { BarChart3, ChevronRight } from 'lucide-react';
import type {
  CohortComparisonView,
  CohortPhaseView,
  RollingExcessView,
  SleeveComparisonView,
} from '@/lib/types';
import { money, signClass } from '@/lib/format';
import { PanelCard } from './PanelCard';
import {
  EmptyState,
  ShortId,
  StatusBadge,
  dateLabel,
  directPct,
  humanize,
  ratioPct,
  scalar,
} from './CohortStatus';

interface Props {
  comparison: CohortComparisonView;
  benchmarkName: string | null;
  benchmarkId: string | null;
  phase: CohortPhaseView | null;
  /** Opens the read-only sleeve detail. Always called with the **stable id**, never the
   *  display name: names repeat across cohorts and would open the wrong sleeve. */
  onOpenSleeve?: (sleeveId: string) => void;
}

function latestRolling(
  row: SleeveComparisonView,
  selectedWindow: string
): RollingExcessView | null {
  if (selectedWindow === 'full') return null;
  const windows = row.rolling_excess[selectedWindow] ?? [];
  return windows.length > 0 ? windows[windows.length - 1] : null;
}

function missingReason(row: SleeveComparisonView): string {
  return row.maturity === 'mature' ? 'Unavailable' : 'Insufficient history';
}

/**
 * Matched cohort comparison.
 *
 * One row per sleeve with the primary metrics; secondary statistics, reliability
 * counts, and exclusions expand per row rather than filling the page.
 */
export function CohortComparisonPanel({
  comparison,
  benchmarkName,
  benchmarkId,
  phase,
  onOpenSleeve,
}: Props) {
  const [selectedWindow, setSelectedWindow] = useState('full');
  const [expanded, setExpanded] = useState<string | null>(null);

  const windows = useMemo(() => {
    const values = new Set<number>();
    for (const row of comparison.sleeves) {
      for (const key of Object.keys(row.rolling_excess)) {
        const parsed = Number(key);
        if (Number.isFinite(parsed)) values.add(parsed);
      }
    }
    return [...values].sort((a, b) => a - b);
  }, [comparison.sleeves]);

  const activeWindow =
    selectedWindow === 'full' || windows.includes(Number(selectedWindow))
      ? selectedWindow
      : 'full';

  if (!comparison.available || comparison.sleeves.length === 0) {
    return (
      <PanelCard title='Matched comparison' icon={<BarChart3 className='h-3.5 w-3.5' />}>
        <EmptyState>
          {comparison.message ??
            'No matched observations exist yet.'}
          {phase?.next_scheduled_session
            ? ` Results appear after the ${dateLabel(
                phase.next_scheduled_session
              )} session completes.`
            : ''}
        </EmptyState>
      </PanelCard>
    );
  }

  return (
    <PanelCard
      title='Matched comparison'
      subtitle={benchmarkName ? `vs ${benchmarkName}` : undefined}
      icon={<BarChart3 className='h-3.5 w-3.5' />}
    >
      <div className='mb-3 flex flex-wrap items-end justify-between gap-3'>
        <p className='text-xs text-muted-foreground'>
          Matched official sessions only. Missing sessions are never treated as zero.
        </p>
        {windows.length > 0 && (
          <label className='text-xs font-medium text-muted-foreground'>
            Window
            <select
              value={activeWindow}
              onChange={(event) => setSelectedWindow(event.target.value)}
              className='ml-2 rounded-md border border-border bg-secondary px-2 py-1 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-primary/50'
            >
              <option value='full'>Full matched history</option>
              {windows.map((window) => (
                <option key={window} value={String(window)}>
                  Latest {window}-session window
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div className='overflow-x-auto'>
        <table className='w-full min-w-[52rem] border-collapse text-sm'>
          <thead>
            <tr className='border-b border-border/60 text-left text-xs font-medium text-muted-foreground'>
              <th scope='col' className='py-2 pr-3 font-medium'>
                Sleeve
              </th>
              <th scope='col' className='py-2 pr-3 font-medium'>
                Maturity
              </th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>
                Sessions
              </th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>
                Return
              </th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>
                {benchmarkName ? `vs ${benchmarkName}` : 'Excess'}
              </th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>
                Max DD
              </th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>
                Turnover
              </th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>
                Coverage
              </th>
              <th scope='col' className='py-2 font-medium'>
                Reliability
              </th>
            </tr>
          </thead>
          <tbody>
            {comparison.sleeves.map((row) => {
              const rolling = latestRolling(row, activeWindow);
              const sleeveReturn =
                activeWindow === 'full' ? row.sleeve_return : rolling?.sleeve_return ?? null;
              const excessReturn =
                activeWindow === 'full' ? row.excess_return : rolling?.excess_return ?? null;
              const benchmarkReturn =
                activeWindow === 'full'
                  ? row.benchmark_return
                  : rolling?.benchmark_return ?? null;
              const isBenchmark = row.sleeve_id === benchmarkId;
              const open = expanded === row.sleeve_id;

              return (
                <Fragment key={row.sleeve_id}>
                  <tr className='border-b border-border/40'>
                    {/* Two controls, deliberately separate: the chevron expands the
                        secondary statistics in place, the name opens the full read-only
                        record. Both are buttons with their own accessible names, so a
                        keyboard user can reach either without guessing. */}
                    <td className='py-2 pr-3'>
                      <div className='flex items-center gap-1.5'>
                        <button
                          type='button'
                          aria-expanded={open}
                          aria-label={`${open ? 'Collapse' : 'Expand'} statistics for ${
                            row.sleeve_name || row.sleeve_id
                          }`}
                          onClick={() => setExpanded(open ? null : row.sleeve_id)}
                          className='rounded text-muted-foreground transition-colors hover:text-primary focus:outline-none focus-visible:ring-1 focus-visible:ring-primary/60'
                        >
                          <ChevronRight
                            className={`h-3.5 w-3.5 shrink-0 transition-transform ${
                              open ? 'rotate-90' : ''
                            }`}
                          />
                        </button>
                        {onOpenSleeve ? (
                          <button
                            type='button'
                            onClick={() => onOpenSleeve(row.sleeve_id)}
                            aria-label={`Open sleeve detail for ${
                              row.sleeve_name || row.sleeve_id
                            }`}
                            className='rounded text-left font-medium text-foreground underline-offset-2 transition-colors hover:text-primary hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-primary/60'
                          >
                            {row.sleeve_name || row.sleeve_id}
                          </button>
                        ) : (
                          <span className='font-medium text-foreground'>
                            {row.sleeve_name || row.sleeve_id}
                          </span>
                        )}
                        {isBenchmark && (
                          <span className='text-xs font-normal text-primary'>benchmark</span>
                        )}
                      </div>
                    </td>
                    <td className='py-2 pr-3'>
                      <StatusBadge status={row.maturity} />
                    </td>
                    <td className='py-2 pr-3 text-right tnum text-foreground'>
                      {row.sample_count}
                    </td>
                    <td className={`py-2 pr-3 text-right tnum ${signClass(sleeveReturn)}`}>
                      {sleeveReturn === null ? missingReason(row) : ratioPct(sleeveReturn, true)}
                    </td>
                    <td className='py-2 pr-3 text-right tnum'>
                      {isBenchmark ? (
                        <span className='text-muted-foreground'>—</span>
                      ) : (
                        <span className={signClass(excessReturn)}>
                          {excessReturn === null
                            ? missingReason(row)
                            : ratioPct(excessReturn, true)}
                        </span>
                      )}
                    </td>
                    <td className='py-2 pr-3 text-right tnum text-foreground'>
                      {directPct(row.max_drawdown_pct)}
                    </td>
                    <td className='py-2 pr-3 text-right tnum text-foreground'>
                      {money(row.total_turnover)}
                    </td>
                    <td className='py-2 pr-3 text-right tnum text-foreground'>
                      {ratioPct(row.coverage)}
                    </td>
                    <td className='py-2 tnum text-muted-foreground'>
                      {row.reliability.used}/{row.reliability.total_observations} used
                    </td>
                  </tr>
                  {open && (
                    <tr className='border-b border-border/40 bg-secondary/15'>
                      <td colSpan={9} className='px-3 py-3'>
                        <dl className='grid gap-x-6 gap-y-2 text-xs sm:grid-cols-3 lg:grid-cols-4'>
                          <Detail label='Strategy' value={row.strategy} />
                          <Detail
                            label='Benchmark return'
                            value={
                              benchmarkReturn === null
                                ? missingReason(row)
                                : ratioPct(benchmarkReturn, true)
                            }
                          />
                          <Detail
                            label='Volatility'
                            value={
                              row.volatility === null ? missingReason(row) : ratioPct(row.volatility)
                            }
                          />
                          <Detail
                            label='Sharpe'
                            value={row.sharpe === null ? missingReason(row) : scalar(row.sharpe)}
                          />
                          <Detail
                            label='Sortino'
                            value={row.sortino === null ? missingReason(row) : scalar(row.sortino)}
                          />
                          <Detail
                            label='Beta'
                            value={row.beta === null ? missingReason(row) : scalar(row.beta)}
                          />
                          <Detail
                            label='Correlation'
                            value={
                              row.correlation === null
                                ? missingReason(row)
                                : scalar(row.correlation)
                            }
                          />
                          <Detail
                            label='Turnover ratio'
                            value={
                              row.turnover_ratio === null
                                ? 'Unavailable'
                                : `${ratioPct(row.turnover_ratio)} of average equity`
                            }
                          />
                          <Detail
                            label='Modeled cost'
                            value={
                              row.total_modeled_cost === null
                                ? 'Unavailable'
                                : `${money(row.total_modeled_cost)} (${
                                    row.modeled_cost_drag === null
                                      ? 'drag unavailable'
                                      : ratioPct(row.modeled_cost_drag)
                                  })`
                            }
                          />
                          <Detail
                            label='Fills / rejections'
                            value={`${row.num_filled} filled, ${row.num_rejected} rejected${
                              row.reject_rate === null ? '' : ` (${ratioPct(row.reject_rate)})`
                            }`}
                          />
                          <Detail
                            label='Observation states'
                            value={`${row.reliability.official} official, ${row.reliability.partial} partial, ${row.reliability.missing} missing`}
                          />
                          <Detail
                            label='Readiness'
                            value={`${row.reliability.readiness_ready} ready, ${row.reliability.readiness_unready} not ready, ${row.reliability.readiness_unknown} unknown`}
                          />
                          <Detail
                            label='Last matched session'
                            value={dateLabel(row.matched_dates.at(-1) ?? null)}
                          />
                          <Detail label='Sleeve id' value={<ShortId value={row.sleeve_id} />} />
                        </dl>

                        {row.reliability.reason_codes.length > 0 && (
                          <p className='mt-3 text-xs text-muted-foreground'>
                            Readiness reasons:{' '}
                            {row.reliability.reason_codes.map(humanize).join(', ')}
                          </p>
                        )}
                        {row.exclusions.length > 0 && (
                          <ul className='mt-2 space-y-0.5 text-xs text-muted-foreground'>
                            {row.exclusions.map((exclusion) => (
                              <li key={`${exclusion.session_date}-${exclusion.reason}`}>
                                Excluded {dateLabel(exclusion.session_date)}:{' '}
                                {humanize(exclusion.reason)}
                              </li>
                            ))}
                          </ul>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </PanelCard>
  );
}

function Detail({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className='min-w-0'>
      <dt className='text-muted-foreground'>{label}</dt>
      <dd className='mt-0.5 tnum break-words text-foreground'>{value}</dd>
    </div>
  );
}
