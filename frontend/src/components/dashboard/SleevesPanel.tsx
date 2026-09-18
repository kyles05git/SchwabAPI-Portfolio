import type { SleeveRow, EquitySeries } from '@/lib/types';
import { money, pct, signClass, n } from '@/lib/format';
import { PanelCard } from './PanelCard';
import { MiniAreaChart } from './MiniAreaChart';
import { SortableTable, type ColumnDef } from './SortableTable';
import { EmptyState, ShortId, TechnicalDetails } from './CohortStatus';
import { History } from 'lucide-react';

interface Props {
  rows: SleeveRow[];
  curves: EquitySeries[];
  /** Opens the read-only sleeve detail, always by **stable id**: these display names are
   *  duplicated across scopes, so a name would be ambiguous by construction. */
  onOpenSleeve?: (sleeveId: string) => void;
}

interface SleeveWithCurve extends SleeveRow {
  curvePoints: number[];
}

/**
 * Historical paper sleeves: unassigned legacy and standalone state.
 *
 * These carry **unmatched lifetime history** from different starting capital and
 * different start dates, so they are deliberately kept out of the official cohort
 * comparison and are never ranked beside cohort members. Excess return appears only
 * when a benchmark was unambiguously resolved *within this group*.
 */
export function SleevesPanel({ rows, curves, onOpenSleeve }: Props) {
  // Curves join on stable id: display names are not unique across scopes.
  const curveById = new Map(curves.map((c) => [c.sleeve_id, c.points]));

  const dataWithCurves: SleeveWithCurve[] = rows.map((r) => ({
    ...r,
    curvePoints: (curveById.get(r.sleeve_id) ?? [])
      .map((point) => n(point))
      .filter((point): point is number => point !== null),
  }));

  const excessBenchmark = rows.find((r) => r.excess_benchmark !== null)?.excess_benchmark ?? null;

  const columns: ColumnDef<SleeveWithCurve>[] = [
    {
      key: 'rank',
      header: '#',
      align: 'left',
      sortable: true,
      sortValue: (row) => row.rank,
      render: (row) => <span className='text-muted-foreground'>{row.rank}</span>,
    },
    {
      key: 'name',
      header: 'Name',
      align: 'left',
      sortable: true,
      sortValue: (row) => row.name,
      // The name is the drill-down affordance, but it navigates by `sleeve_id`. The
      // label an operator reads and the identity the request carries are different
      // things here, and this table is precisely where those two diverge.
      render: (row) =>
        onOpenSleeve ? (
          <button
            type='button'
            onClick={() => onOpenSleeve(row.sleeve_id)}
            aria-label={`Open sleeve detail for ${row.name}`}
            className={`rounded text-left font-medium underline-offset-2 transition-colors hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-primary/60 ${
              row.is_benchmark ? 'text-primary' : 'text-foreground hover:text-primary'
            }`}
          >
            {row.name}
          </button>
        ) : (
          <span className={`font-medium ${row.is_benchmark ? 'text-primary' : 'text-foreground'}`}>
            {row.name}
          </span>
        ),
    },
    {
      key: 'strategy',
      header: 'Strategy',
      align: 'left',
      render: (row) => <span className='text-muted-foreground'>{row.strategy}</span>,
    },
    {
      key: 'starting_capital',
      header: 'Start capital',
      align: 'right',
      sortable: true,
      sortValue: (row) => n(row.starting_capital) ?? 0,
      render: (row) => (
        <span className='text-muted-foreground'>{money(row.starting_capital)}</span>
      ),
    },
    {
      key: 'trades',
      header: 'Trades',
      align: 'right',
      sortable: true,
      sortValue: (row) => row.trades,
      render: (row) => String(row.trades),
    },
    {
      key: 'value',
      header: 'Value',
      align: 'right',
      sortable: true,
      sortValue: (row) => n(row.value) ?? 0,
      render: (row) => money(row.value),
    },
    {
      key: 'return_pct',
      header: 'Return',
      align: 'right',
      sortable: true,
      sortValue: (row) => n(row.return_pct) ?? 0,
      render: (row) => (
        <span className={signClass(row.return_pct)}>{pct(row.return_pct, true)}</span>
      ),
    },
  ];

  if (excessBenchmark !== null) {
    columns.push({
      key: 'excess_pct',
      header: `vs ${excessBenchmark}`,
      align: 'right',
      sortable: true,
      sortValue: (row) => n(row.excess_pct) ?? 0,
      render: (row) =>
        row.is_benchmark || row.excess_pct === null ? (
          <span className='text-muted-foreground'>—</span>
        ) : (
          <span className={signClass(row.excess_pct)}>{pct(row.excess_pct, true)}</span>
        ),
    });
  }

  columns.push(
    {
      key: 'max_dd',
      header: 'Max DD',
      align: 'right',
      sortable: true,
      sortValue: (row) => n(row.max_drawdown_pct) ?? Number.NEGATIVE_INFINITY,
      render: (row) => {
        const value = n(row.max_drawdown_pct);
        return value === null ? (
          <span className='text-muted-foreground'>Unavailable</span>
        ) : (
          <span className='text-loss'>-{value.toFixed(1)}%</span>
        );
      },
    },
    {
      key: 'sharpe',
      header: 'Sharpe',
      align: 'right',
      sortable: true,
      sortValue: (row) => n(row.sharpe) ?? 0,
      render: (row) =>
        row.sharpe !== null ? (
          <span className='text-foreground'>{String(n(row.sharpe))}</span>
        ) : (
          <span className='text-muted-foreground'>—</span>
        ),
    },
    {
      key: 'equity',
      header: 'Equity',
      align: 'right',
      render: (row) => (
        <div className='ml-auto w-[140px]'>
          <MiniAreaChart points={row.curvePoints} height={32} name={row.name} />
        </div>
      ),
    }
  );

  return (
    <PanelCard
      title='Historical paper sleeves'
      subtitle={`${rows.length} sleeves`}
      icon={<History className='h-3.5 w-3.5' />}
    >
      <p className='mb-3 text-xs leading-5 text-muted-foreground'>
        Unmatched lifetime history from sleeves outside the official cohort. Starting
        capital, start dates, and strategy definitions differ between these rows, so they
        are not comparable to official cohort results and are not ranked against them.
        {excessBenchmark === null &&
          ' No benchmark was unambiguously resolvable in this group, so excess return is not shown.'}
      </p>

      {rows.length === 0 ? (
        <EmptyState>No historical paper sleeves exist.</EmptyState>
      ) : (
        <>
          <SortableTable
            columns={columns}
            data={dataWithCurves}
            emptyMessage='No historical paper sleeves exist.'
          />
          <TechnicalDetails className='mt-3' label='Stable sleeve identifiers'>
            <dl className='grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2'>
              {rows.map((row) => (
                <div key={row.sleeve_id} className='flex flex-wrap items-center gap-2'>
                  <dt className='text-muted-foreground'>{row.name}</dt>
                  <dd>
                    <ShortId value={row.sleeve_id} />
                  </dd>
                </div>
              ))}
            </dl>
          </TechnicalDetails>
        </>
      )}
    </PanelCard>
  );
}
