import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import type {
  Num,
  ObservationRowView,
  PageInfo,
  SimulatedOrderRowView,
  SleeveDetail,
  SleeveLifecycleView,
  SleevePositionsView,
  SleeveScope,
} from '@/lib/types';
import { money, pct, signClass } from '@/lib/format';
import { useSleeveDetail, type SleeveDetailState } from '@/lib/useSleeveDetail';
import { MiniAreaChart } from './MiniAreaChart';
import {
  EmptyState,
  Metric,
  ShortId,
  StatusBadge,
  TechnicalDetails,
  dateLabel,
  dateTimeLabel,
  directPct,
  humanize,
  humanizeTitle,
  ratioPct,
  scalar,
} from './CohortStatus';

interface Props {
  /** Stable sleeve identity. `null` closes the drawer. Never a display name. */
  sleeveId: string | null;
  onClose: () => void;
  /** Dev fixture mode: resolve from committed offline fixtures instead of the server. */
  useFixtures?: boolean;
}

const SCOPE_LABELS: Record<SleeveScope, string> = {
  'official-cohort': 'Official cohort member',
  legacy: 'Legacy sleeve (pre-cohort history)',
  standalone: 'Standalone sleeve (no history yet)',
};

/**
 * Read-only detail for one paper sleeve, opened from a dashboard row.
 *
 * Scoped entirely by stable identity: the caller passes an id, never a name, because the
 * same display name legitimately exists in an active cohort and in the superseded cohort
 * it replaced. The heading shows the name, the identity stays visible beside it, and the
 * two are never confused for one another.
 *
 * There is deliberately no run, order, reset, repair, or replay control anywhere in here.
 * A superseded sleeve renders in full and is labelled closed.
 */
export function SleeveDetailDrawer({ sleeveId, onClose, useFixtures = false }: Props) {
  const { state, reload } = useSleeveDetail(sleeveId, { useFixtures });
  const panelRef = useRef<HTMLDivElement | null>(null);
  const restoreFocusTo = useRef<HTMLElement | null>(null);

  // Remember what opened the drawer so focus can go back there on close: an operator
  // who tabbed to a row and pressed Enter must not be dumped at the top of the document.
  useEffect(() => {
    if (sleeveId) {
      restoreFocusTo.current = document.activeElement as HTMLElement | null;
      panelRef.current?.focus();
      return;
    }
    restoreFocusTo.current?.focus?.();
  }, [sleeveId]);

  useEffect(() => {
    if (!sleeveId) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKeyDown);
    // Stop the page scrolling behind the record while it is open.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [sleeveId, onClose]);

  if (!sleeveId) return null;

  const title = state.status === 'ready' ? state.detail.identity.name : 'Sleeve detail';

  // Rendered through a portal to `document.body`, not in place. The app shell wraps its
  // children in `animate-fade-in`, whose `forwards` fill leaves a `transform` on the
  // wrapper — and a transformed ancestor becomes the containing block for
  // `position: fixed`. In place, the drawer was therefore trapped inside the scrolling
  // <main> and clipped by the status bar instead of covering the viewport.
  return createPortal(
    <div className='fixed inset-0 z-50 flex justify-end'>
      {/* Scrim. A click closes, which is why it carries an accessible label rather than
          being an unreachable div: keyboard users have Escape, pointer users have this. */}
      <button
        type='button'
        aria-label='Close sleeve detail'
        onClick={onClose}
        className='absolute inset-0 bg-background/70 backdrop-blur-sm'
      />
      <div
        ref={panelRef}
        role='dialog'
        aria-modal='true'
        aria-labelledby='sleeve-detail-title'
        tabIndex={-1}
        className='relative flex h-full w-full max-w-3xl flex-col overflow-y-auto border-l border-border bg-card shadow-2xl focus:outline-none'
      >
        <header className='sticky top-0 z-10 flex items-start gap-3 border-b border-border bg-card/95 px-5 py-4 backdrop-blur'>
          <div className='min-w-0 flex-1'>
            <p className='text-[11px] font-semibold uppercase tracking-wider text-muted-foreground'>
              Paper sleeve — read only
            </p>
            <h2
              id='sleeve-detail-title'
              className='mt-0.5 truncate text-lg font-semibold text-foreground'
            >
              {title}
            </h2>
            {state.status === 'ready' && <HeaderMeta detail={state.detail} />}
          </div>
          <button
            type='button'
            onClick={onClose}
            aria-label='Close sleeve detail'
            className='rounded-md border border-border p-1.5 text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-primary/60'
          >
            <X className='h-4 w-4' />
          </button>
        </header>

        <div className='flex-1 space-y-5 px-5 py-5'>
          <Body state={state} onRetry={reload} />
        </div>
      </div>
    </div>,
    document.body
  );
}

function HeaderMeta({ detail }: { detail: SleeveDetail }) {
  const { identity } = detail;
  return (
    <div className='mt-2 flex flex-wrap items-center gap-2 text-xs'>
      <StatusBadge status={identity.scope} label={SCOPE_LABELS[identity.scope]} />
      {identity.lifecycle.historical && (
        <StatusBadge status='failed' label={identity.lifecycle.label || 'Superseded'} />
      )}
      {identity.cohort_id && (
        <span className='text-muted-foreground'>Cohort {identity.cohort_id}</span>
      )}
      <span className='text-muted-foreground'>
        ID <ShortId value={identity.sleeve_id} />
      </span>
    </div>
  );
}

function Body({
  state,
  onRetry,
}: {
  state: SleeveDetailState;
  onRetry: () => void;
}) {
  if (state.status === 'idle' || state.status === 'loading') {
    return (
      <div aria-busy='true' aria-live='polite' className='space-y-3'>
        <p className='text-sm text-muted-foreground'>Loading sleeve record…</p>
        {[0, 1, 2].map((row) => (
          <div key={row} className='h-20 animate-pulse rounded-md border border-border/60 bg-secondary/20' />
        ))}
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <div role='alert' className='space-y-3'>
        <EmptyState tone='pending'>{state.message}</EmptyState>
        {/* Only offered for a transport failure. Retrying an `unknown-sleeve` or a
            malformed id would just fail again the same way, and inviting it implies the
            record might appear, which it will not. */}
        {state.code === 'request-failed' || state.code === 'registry-unavailable' ? (
          <button
            type='button'
            onClick={onRetry}
            className='rounded-md border border-border px-3 py-1.5 text-sm text-foreground transition-colors hover:bg-secondary focus:outline-none focus-visible:ring-1 focus-visible:ring-primary/60'
          >
            Try again
          </button>
        ) : null}
      </div>
    );
  }

  const { detail } = state;
  return (
    <>
      {detail.warnings.length > 0 && (
        <ul className='space-y-2' aria-label='Record warnings'>
          {detail.warnings.map((warning) => (
            <li key={warning}>
              <EmptyState tone='pending'>{warning}</EmptyState>
            </li>
          ))}
        </ul>
      )}

      <LifecycleSection lifecycle={detail.identity.lifecycle} />
      <ValuationSection detail={detail} />
      <PositionsSection positions={detail.positions} />
      <EquitySection detail={detail} />
      <StrategySection detail={detail} />
      <ActivitySection detail={detail} />
      <ObservationsSection detail={detail} />
      <LineageSection detail={detail} />
    </>
  );
}

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className='rounded-lg border border-border bg-background/40' aria-label={title}>
      <div className='border-b border-border/60 px-4 py-2.5'>
        <h3 className='text-xs font-semibold uppercase tracking-wider text-muted-foreground'>
          {title}
        </h3>
        {subtitle && <p className='mt-1 text-xs text-muted-foreground'>{subtitle}</p>}
      </div>
      <div className='px-4 py-3.5'>{children}</div>
    </section>
  );
}

function LifecycleSection({ lifecycle }: { lifecycle: SleeveLifecycleView }) {
  if (!lifecycle.historical) return null;
  return (
    <Section title='Closed record'>
      <p className='text-sm text-foreground'>{lifecycle.reason}</p>
      <dl className='mt-3 grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2'>
        <Field label='Lifecycle' value={humanizeTitle(lifecycle.lifecycle)} />
        <Field label='Replaced by' value={lifecycle.superseded_by ?? 'Not recorded'} />
        <Field label='Actions available' value='None — this view is read only' />
        {lifecycle.reference && <Field label='Full record' value={lifecycle.reference} />}
      </dl>
    </Section>
  );
}

/** Format a recorded return using the unit the server said it is in. */
function recordedReturn(value: Num | null, basis: string | null): string {
  if (value === null) return 'Unavailable';
  return basis === 'ratio' ? ratioPct(value, true) : pct(value, true);
}

function ValuationSection({ detail }: { detail: SleeveDetail }) {
  const { recorded_valuation: recorded, cash, performance, capital } = detail;
  return (
    <Section
      title='Cash, equity, and P&L'
      subtitle='Recorded values only. This view never requests a quote, so no figure here is a live mark.'
    >
      <div className='grid gap-4 sm:grid-cols-2 lg:grid-cols-3'>
        <Metric
          label='Recorded equity'
          emphasis
          value={recorded.available ? money(recorded.total_equity) : 'Unavailable'}
          detail={
            recorded.available
              ? `${humanize(recorded.source ?? 'recorded')} · as of ${
                  recorded.session_date
                    ? dateLabel(recorded.session_date)
                    : dateTimeLabel(recorded.as_of)
                }`
              : recorded.message
          }
        />
        <Metric
          label='Recorded return'
          value={
            <span className={signClass(recorded.return_pct)}>
              {recordedReturn(recorded.return_pct, recorded.return_pct_basis)}
            </span>
          }
          detail={recorded.status ? `Observation status: ${humanize(recorded.status)}` : undefined}
        />
        <Metric label='Starting capital' value={money(capital.starting_capital)} />
        <Metric
          label='Settled cash'
          value={cash.available ? money(cash.settled_cash) : 'Unavailable'}
          detail={cash.available ? `as of ${dateTimeLabel(cash.as_of)}` : cash.message}
        />
        <Metric
          label='Unsettled cash'
          value={cash.available ? money(cash.unsettled_cash) : 'Unavailable'}
          detail={`Settlement model: ${capital.settlement_model}`}
        />
        <Metric
          label='Realized P&L'
          value={
            cash.available ? (
              <span className={signClass(cash.realized_pnl)}>{money(cash.realized_pnl)}</span>
            ) : (
              'Unavailable'
            )
          }
        />
        <Metric
          label='Unrealized P&L'
          value={
            recorded.unrealized_pnl === null ? (
              'Unavailable'
            ) : (
              <span className={signClass(recorded.unrealized_pnl)}>
                {money(recorded.unrealized_pnl)}
              </span>
            )
          }
          detail={
            recorded.unrealized_pnl === null
              ? 'Needs a current mark, which is not recorded'
              : undefined
          }
        />
        <Metric
          label='Buying power'
          value={cash.available ? money(cash.buying_power) : 'Unavailable'}
          detail={`Leverage ${scalar(capital.leverage)}×`}
        />
        <Metric
          label='Lifetime return'
          value={
            performance.available ? (
              <span className={signClass(performance.total_return_pct)}>
                {directPct(performance.total_return_pct)}
              </span>
            ) : (
              'Unavailable'
            )
          }
          detail={
            performance.available
              ? `${performance.cycles} cycles · ${performance.trades_filled} fills · max DD ${directPct(
                  performance.max_drawdown_pct
                )}`
              : performance.message
          }
        />
      </div>
    </Section>
  );
}

function PositionsSection({ positions }: { positions: SleevePositionsView }) {
  if (!positions.available) {
    return (
      <Section title='Positions and allocation'>
        <EmptyState>{positions.message ?? 'Positions are unavailable.'}</EmptyState>
      </Section>
    );
  }
  if (positions.rows.length === 0) {
    return (
      <Section title='Positions and allocation'>
        <EmptyState>
          {positions.message ?? 'This sleeve currently holds no paper positions.'}
        </EmptyState>
      </Section>
    );
  }
  return (
    <Section
      title='Positions and allocation'
      subtitle={`Recorded as of ${dateTimeLabel(positions.as_of)}. ${positions.mark_note}`}
    >
      <div className='overflow-x-auto'>
        <table className='w-full min-w-[36rem] border-collapse text-sm'>
          <caption className='sr-only'>
            Current paper positions with quantity, average cost, cost basis, and
            cost-basis weight
          </caption>
          <thead>
            <tr className='border-b border-border/60 text-left text-xs font-medium text-muted-foreground'>
              <th scope='col' className='py-2 pr-3 font-medium'>Symbol</th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>Quantity</th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>Avg cost</th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>Cost basis</th>
              <th scope='col' className='py-2 pr-3 text-right font-medium'>Weight</th>
              <th scope='col' className='py-2 text-right font-medium'>Market value</th>
            </tr>
          </thead>
          <tbody>
            {positions.rows.map((row) => (
              <tr key={row.symbol} className='border-b border-border/40'>
                <th scope='row' className='py-2 pr-3 text-left font-medium text-foreground'>
                  {row.symbol}
                </th>
                <td className='py-2 pr-3 text-right tnum text-foreground'>{row.quantity}</td>
                <td className='py-2 pr-3 text-right tnum text-foreground'>
                  {money(row.average_cost)}
                </td>
                <td className='py-2 pr-3 text-right tnum text-foreground'>
                  {money(row.cost_basis)}
                </td>
                <td className='py-2 pr-3 text-right tnum text-muted-foreground'>
                  {ratioPct(row.cost_basis_weight)}
                </td>
                <td className='py-2 text-right tnum text-muted-foreground'>Unavailable</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope='row' className='py-2 pr-3 text-left text-xs font-medium text-muted-foreground'>
                Total cost basis
              </th>
              <td colSpan={2} />
              <td className='py-2 pr-3 text-right tnum font-semibold text-foreground'>
                {money(positions.total_cost_basis)}
              </td>
              <td colSpan={2} />
            </tr>
          </tfoot>
        </table>
      </div>
      <p className='mt-2 text-xs text-muted-foreground'>
        Weight is a share of total <em>cost basis</em>, not of market value.
      </p>
    </Section>
  );
}

function EquitySection({ detail }: { detail: SleeveDetail }) {
  const history = detail.equity_history;
  if (!history.available || history.points.length === 0) {
    return (
      <Section title='Equity history'>
        <EmptyState>{history.message ?? 'No equity values have been recorded.'}</EmptyState>
      </Section>
    );
  }
  const points = history.points.map((point) => point.total_value);
  const first = history.points[0];
  const last = history.points[history.points.length - 1];
  return (
    <Section
      title='Equity history'
      subtitle={`${humanize(history.source ?? 'recorded')} · ${
        history.page?.returned ?? points.length
      } of ${history.page?.available ?? points.length} recorded values${
        history.page?.truncated ? ' (count is a floor: the scan limit was reached)' : ''
      }`}
    >
      {history.message && <p className='mb-3 text-xs text-muted-foreground'>{history.message}</p>}
      <MiniAreaChart points={points} height={120} name='Recorded equity' />
      <dl className='mt-3 grid gap-x-6 gap-y-2 text-xs sm:grid-cols-3'>
        <Field
          label='Window starts'
          value={`${money(first.total_value)} · ${
            first.session_date ? dateLabel(first.session_date) : dateTimeLabel(first.as_of)
          }`}
        />
        <Field
          label='Window ends'
          value={`${money(last.total_value)} · ${
            last.session_date ? dateLabel(last.session_date) : dateTimeLabel(last.as_of)
          }`}
        />
        <Field label='Values in window' value={String(points.length)} />
      </dl>
    </Section>
  );
}

function StrategySection({ detail }: { detail: SleeveDetail }) {
  const { strategy, capital } = detail;
  const parameters = Object.entries(strategy.parameters);
  return (
    <Section title='Strategy and configuration'>
      {!strategy.reproducible && strategy.message && (
        <div className='mb-3'>
          <EmptyState>{strategy.message}</EmptyState>
        </div>
      )}
      <dl className='grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-3'>
        <Field label='Strategy' value={strategy.strategy} />
        <Field label='Implementation' value={strategy.implementation_name ?? 'Not recorded'} />
        <Field
          label='Version'
          value={
            strategy.strategy_version
              ? `${strategy.strategy_id ?? ''} ${strategy.strategy_version}`.trim()
              : 'Not recorded'
          }
        />
        <Field label='Reproducible' value={strategy.reproducible ? 'Yes' : 'No — parameters were never captured'} />
        <Field label='Cadence' value={`${humanize(strategy.decision_frequency) || 'Not recorded'} at ${strategy.decision_time || 'not recorded'}`} />
        <Field label='Benchmark' value={strategy.benchmark_symbol_or_sleeve ?? 'Not recorded'} />
        <Field label='Max positions' value={String(capital.max_positions)} />
        <Field label='Max position fraction' value={ratioPct(capital.max_position_fraction)} />
        <Field label='Long only' value={strategy.long_only === null ? 'Not recorded' : strategy.long_only ? 'Yes' : 'No'} />
        <Field
          label='Leverage allowed'
          value={strategy.leverage_allowed === null ? 'Not recorded' : strategy.leverage_allowed ? 'Yes' : 'No'}
        />
        <Field label='Configuration hash' value={<ShortId value={strategy.configuration_hash} />} />
        {strategy.factor && <Field label='Factor' value={strategy.factor} />}
      </dl>

      {strategy.universe.length > 0 && (
        <p className='mt-3 text-xs text-muted-foreground'>
          Universe: <span className='text-foreground'>{strategy.universe.join(', ')}</span>
        </p>
      )}
      {strategy.data_requirements.length > 0 && (
        <p className='mt-1 text-xs text-muted-foreground'>
          Data requirements:{' '}
          <span className='text-foreground'>{strategy.data_requirements.map(humanize).join(', ')}</span>
        </p>
      )}

      {parameters.length > 0 && (
        <TechnicalDetails className='mt-3' label='Normalized parameters'>
          <dl className='grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2'>
            {parameters.map(([key, value]) => (
              <div key={key} className='min-w-0'>
                <dt className='text-muted-foreground'>{humanize(key)}</dt>
                <dd className='tnum break-words text-foreground'>{formatValue(value)}</dd>
              </div>
            ))}
          </dl>
        </TechnicalDetails>
      )}
    </Section>
  );
}

function ActivitySection({ detail }: { detail: SleeveDetail }) {
  const { simulated_orders: orders, cycles } = detail;
  return (
    <Section
      title='Recent activity'
      subtitle='Simulated paper orders and recorded valuation cycles. Nothing here reached a broker.'
    >
      <h4 className='text-xs font-semibold text-foreground'>Simulated orders</h4>
      {!orders.available || orders.rows.length === 0 ? (
        <div className='mt-2'>
          <EmptyState>{orders.message ?? 'No simulated orders are recorded.'}</EmptyState>
        </div>
      ) : (
        <div className='mt-2 overflow-x-auto'>
          <table className='w-full min-w-[38rem] border-collapse text-sm'>
            <caption className='sr-only'>
              Recent simulated paper orders, newest first, with fills and rejection reasons
            </caption>
            <thead>
              <tr className='border-b border-border/60 text-left text-xs font-medium text-muted-foreground'>
                <th scope='col' className='py-2 pr-3 font-medium'>Recorded</th>
                <th scope='col' className='py-2 pr-3 font-medium'>Order</th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>Limit</th>
                <th scope='col' className='py-2 pr-3 font-medium'>Status</th>
                <th scope='col' className='py-2 font-medium'>Fill / reason</th>
              </tr>
            </thead>
            <tbody>
              {orders.rows.map((row) => (
                <tr key={row.paper_order_id} className='border-b border-border/40'>
                  <td className='py-2 pr-3 text-muted-foreground'>{dateTimeLabel(row.as_of)}</td>
                  <th scope='row' className='py-2 pr-3 text-left font-medium text-foreground'>
                    {row.side} {row.quantity} {row.symbol}
                  </th>
                  <td className='py-2 pr-3 text-right tnum text-foreground'>
                    {money(row.limit_price)}
                  </td>
                  <td className='py-2 pr-3'>
                    <StatusBadge status={orderTone(row)} label={humanizeTitle(row.status)} />
                  </td>
                  <td className='py-2 text-xs text-muted-foreground'>
                    {row.fill_price !== null
                      ? `Filled at ${money(row.fill_price)}`
                      : row.reason ?? 'No reason recorded'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {orders.page && <PageNote label='orders' page={orders.page} />}

      <h4 className='mt-5 text-xs font-semibold text-foreground'>Valuation cycles</h4>
      {!cycles.available || cycles.rows.length === 0 ? (
        <div className='mt-2'>
          <EmptyState>{cycles.message ?? 'No valuation cycles are recorded.'}</EmptyState>
        </div>
      ) : (
        <div className='mt-2 overflow-x-auto'>
          <table className='w-full min-w-[38rem] border-collapse text-sm'>
            <caption className='sr-only'>
              Recorded valuation cycles, newest first, with proposal, fill, and rejection counts
            </caption>
            <thead>
              <tr className='border-b border-border/60 text-left text-xs font-medium text-muted-foreground'>
                <th scope='col' className='py-2 pr-3 font-medium'>Recorded</th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>Proposed</th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>Filled</th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>Rejected</th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>Equity</th>
                <th scope='col' className='py-2 text-right font-medium'>Return</th>
              </tr>
            </thead>
            <tbody>
              {cycles.rows.map((row) => (
                <tr key={row.cycle_id} className='border-b border-border/40'>
                  <th scope='row' className='py-2 pr-3 text-left font-normal text-muted-foreground'>
                    {dateTimeLabel(row.as_of)}
                  </th>
                  <td className='py-2 pr-3 text-right tnum text-foreground'>{row.num_proposals}</td>
                  <td className='py-2 pr-3 text-right tnum text-foreground'>{row.num_filled}</td>
                  <td className='py-2 pr-3 text-right tnum text-foreground'>{row.num_rejected}</td>
                  <td className='py-2 pr-3 text-right tnum text-foreground'>
                    {money(row.total_value)}
                  </td>
                  <td className={`py-2 text-right tnum ${signClass(row.return_pct)}`}>
                    {directPct(row.return_pct)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {cycles.page && <PageNote label='cycles' page={cycles.page} />}
      {detail.lineage.notes.length > 0 && (
        <ul className='mt-3 space-y-1 text-xs text-muted-foreground'>
          {detail.lineage.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function ObservationsSection({ detail }: { detail: SleeveDetail }) {
  const { observations, runs } = detail;
  return (
    <Section
      title='Official observations and runs'
      subtitle='The cohort record supporting every figure above.'
    >
      {!observations.available || observations.rows.length === 0 ? (
        <EmptyState>{observations.message ?? 'No official observations are recorded.'}</EmptyState>
      ) : (
        <div className='overflow-x-auto'>
          <table className='w-full min-w-[42rem] border-collapse text-sm'>
            <caption className='sr-only'>
              Official daily observations for this sleeve, newest session first
            </caption>
            <thead>
              <tr className='border-b border-border/60 text-left text-xs font-medium text-muted-foreground'>
                <th scope='col' className='py-2 pr-3 font-medium'>Session</th>
                <th scope='col' className='py-2 pr-3 font-medium'>Status</th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>Equity</th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>Return</th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>Turnover</th>
                <th scope='col' className='py-2 pr-3 text-right font-medium'>Coverage</th>
                <th scope='col' className='py-2 font-medium'>Readiness</th>
              </tr>
            </thead>
            <tbody>
              {observations.rows.map((row) => (
                <tr key={`${row.session_date}-${row.run_id}`} className='border-b border-border/40'>
                  <th scope='row' className='py-2 pr-3 text-left font-medium text-foreground'>
                    {dateLabel(row.session_date)}
                    <span className='ml-1 block text-[11px] font-normal text-muted-foreground'>
                      valued {dateTimeLabel(row.as_of)}
                    </span>
                  </th>
                  <td className='py-2 pr-3'>
                    <StatusBadge status={row.status} />
                  </td>
                  <td className='py-2 pr-3 text-right tnum text-foreground'>
                    {row.total_value === null ? 'Unavailable' : money(row.total_value)}
                  </td>
                  <td className={`py-2 pr-3 text-right tnum ${signClass(row.return_pct)}`}>
                    {ratioPct(row.return_pct, true)}
                  </td>
                  <td className='py-2 pr-3 text-right tnum text-foreground'>
                    {money(row.turnover)}
                  </td>
                  <td className='py-2 pr-3 text-right tnum text-foreground'>
                    {ratioPct(row.quote_coverage)}
                  </td>
                  <td className='py-2 text-xs text-muted-foreground'>
                    {readinessLabel(row)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {observations.page && <PageNote label='observations' page={observations.page} />}

      <h4 className='mt-5 text-xs font-semibold text-foreground'>Cohort runs</h4>
      {!runs.available || runs.rows.length === 0 ? (
        <div className='mt-2'>
          <EmptyState>{runs.message ?? 'No cohort runs reference this sleeve.'}</EmptyState>
        </div>
      ) : (
        <ul className='mt-2 space-y-2'>
          {runs.rows.map((run) => (
            <li
              key={run.run_id}
              className='rounded-md border border-border/60 bg-secondary/10 px-3 py-2 text-xs'
            >
              <div className='flex flex-wrap items-center gap-2'>
                <span className='font-medium text-foreground'>{dateLabel(run.scheduled_for)}</span>
                <StatusBadge status={run.status} />
                {run.member_status && (
                  <StatusBadge status={run.member_status} label={`This sleeve: ${humanize(run.member_status)}`} />
                )}
              </div>
              {run.member_error_message && (
                <p className='mt-1 text-loss'>
                  {run.member_error_code ? `${humanize(run.member_error_code)}: ` : ''}
                  {run.member_error_message}
                </p>
              )}
              <TechnicalDetails className='mt-1' label='Run identifiers'>
                <dl className='grid gap-x-4 gap-y-1 sm:grid-cols-2'>
                  <Field label='Run id' value={<ShortId value={run.run_id} chars={16} />} />
                  <Field label='Session id' value={run.session_id} />
                  <Field label='Cohort snapshot' value={<ShortId value={run.snapshot_id} chars={16} />} />
                  <Field label='Quote snapshot' value={<ShortId value={run.quote_snapshot_id} chars={16} />} />
                </dl>
              </TechnicalDetails>
            </li>
          ))}
        </ul>
      )}
      {runs.page && <PageNote label='runs' page={runs.page} />}
    </Section>
  );
}

function LineageSection({ detail }: { detail: SleeveDetail }) {
  const { lineage, identity } = detail;
  const snapshots = Object.entries(lineage.snapshot_ids);
  return (
    <Section title='Lineage and technical details'>
      <dl className='grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2'>
        <Field label='Stable sleeve id' value={<ShortId value={identity.sleeve_id} chars={16} />} />
        <Field label='Storage namespace' value={<ShortId value={lineage.namespace_id} chars={16} />} />
        <Field label='Cohort' value={lineage.cohort_id ?? 'Unassigned'} />
        <Field
          label='Cohort start session'
          value={lineage.cohort_start_session ? dateLabel(lineage.cohort_start_session) : 'Not recorded'}
        />
        <Field label='Configuration hash' value={<ShortId value={lineage.configuration_hash} chars={16} />} />
        <Field label='Sleeve created' value={dateTimeLabel(identity.created_at)} />
        <Field label='Original name' value={identity.original_name || identity.name} />
        <Field label='Contract version' value={detail.contract_version} />
      </dl>

      {lineage.strategy_hashes.length > 1 && (
        <div className='mt-3'>
          <EmptyState tone='pending'>
            This sleeve&apos;s observations reference {lineage.strategy_hashes.length} different
            strategy hashes, so its recorded definition changed during the collection.
          </EmptyState>
        </div>
      )}

      {snapshots.length > 0 && (
        <TechnicalDetails className='mt-3' label='Latest snapshot and readiness identities'>
          <dl className='grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2'>
            {snapshots.map(([key, value]) => (
              <div key={key} className='min-w-0'>
                <dt className='text-muted-foreground'>{humanize(key)}</dt>
                <dd className='break-words text-foreground'>{value}</dd>
              </div>
            ))}
          </dl>
        </TechnicalDetails>
      )}

      <p className='mt-3 text-xs text-muted-foreground'>
        Record generated {dateTimeLabel(detail.generated_at)}. This view is read only and
        exposes no run, order, or repair action.
      </p>
    </Section>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className='min-w-0'>
      <dt className='text-muted-foreground'>{label}</dt>
      <dd className='mt-0.5 tnum break-words text-foreground'>{value}</dd>
    </div>
  );
}

function PageNote({ label, page }: { label: string; page: PageInfo }) {
  return (
    <p className='mt-2 text-xs text-muted-foreground'>
      Showing the most recent {page.returned} of {page.available} recorded {label}
      {page.truncated ? ' (count is a floor: the scan limit was reached)' : ''}
      {page.has_more ? '. Older records exist beyond this window.' : '.'}
    </p>
  );
}

function orderTone(row: SimulatedOrderRowView): string {
  return row.status.toLowerCase() === 'filled' ? 'completed' : 'failed';
}

function readinessLabel(row: ObservationRowView): string {
  if (row.readiness_ready === null) return 'Not recorded';
  if (row.readiness_ready) return 'Ready';
  return row.readiness_reasons.length > 0
    ? `Not ready — ${row.readiness_reasons.map(humanize).join(', ')}`
    : 'Not ready';
}

/** Render a normalized configuration value without pretending a nested object is text. */
function formatValue(value: unknown): string {
  if (value === null || value === undefined) return 'Not recorded';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
