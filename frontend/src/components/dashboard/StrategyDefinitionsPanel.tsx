import { Fragment, useState } from 'react';
import { Braces, ChevronRight } from 'lucide-react';
import type { CohortSleeveView } from '@/lib/types';
import { PanelCard } from './PanelCard';
import { EmptyState, ShortId, StatusBadge, humanize } from './CohortStatus';

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'Unavailable';
  if (Array.isArray(value)) return value.map(displayValue).join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/**
 * Experiment configuration.
 *
 * Definitions matter for reproducibility but must not dominate the operator view, so
 * each sleeve is one compact row that expands to the full parameters and hash.
 */
export function StrategyDefinitionsPanel({
  definitions,
  benchmarkId,
}: {
  definitions: CohortSleeveView[];
  benchmarkId: string | null;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <PanelCard title='Experiment configuration' icon={<Braces className='h-3.5 w-3.5' />}>
      {definitions.length === 0 ? (
        <EmptyState tone='pending'>
          Persisted strategy-definition details are unavailable.
        </EmptyState>
      ) : (
        <div className='overflow-x-auto'>
          <table className='w-full min-w-[40rem] border-collapse text-sm'>
            <thead>
              <tr className='border-b border-border/60 text-left text-xs font-medium text-muted-foreground'>
                <th scope='col' className='py-2 pr-3 font-medium'>
                  Sleeve
                </th>
                <th scope='col' className='py-2 pr-3 font-medium'>
                  Strategy
                </th>
                <th scope='col' className='py-2 pr-3 font-medium'>
                  Version
                </th>
                <th scope='col' className='py-2 pr-3 font-medium'>
                  Reproducible
                </th>
                <th scope='col' className='py-2 font-medium'>
                  Configuration hash
                </th>
              </tr>
            </thead>
            <tbody>
              {definitions.map((item) => {
                const definition = asRecord(item.definition);
                const parameters = asRecord(definition.parameters);
                const requirements = Array.isArray(definition.data_requirements)
                  ? definition.data_requirements
                  : [];
                const open = expanded === item.sleeve_id;

                return (
                  <Fragment key={item.sleeve_id}>
                    <tr className='border-b border-border/40'>
                      <td className='py-2 pr-3'>
                        <button
                          type='button'
                          aria-expanded={open}
                          onClick={() => setExpanded(open ? null : item.sleeve_id)}
                          className='flex items-center gap-1.5 rounded text-left font-medium text-foreground transition-colors hover:text-primary focus:outline-none focus-visible:ring-1 focus-visible:ring-primary/60'
                        >
                          <ChevronRight
                            className={`h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform ${
                              open ? 'rotate-90' : ''
                            }`}
                          />
                          {item.sleeve_name || item.sleeve_id}
                          {item.sleeve_id === benchmarkId && (
                            <span className='text-xs font-normal text-primary'>benchmark</span>
                          )}
                        </button>
                      </td>
                      <td className='py-2 pr-3 text-muted-foreground'>{item.strategy}</td>
                      <td className='py-2 pr-3 tnum text-muted-foreground'>
                        {displayValue(definition.strategy_version)}
                      </td>
                      <td className='py-2 pr-3'>
                        <StatusBadge
                          status={item.reproducible ? 'ready' : 'fail'}
                          label={item.reproducible ? 'Yes' : 'No'}
                        />
                      </td>
                      <td className='py-2'>
                        <ShortId value={item.configuration_hash} />
                      </td>
                    </tr>
                    {open && (
                      <tr className='border-b border-border/40 bg-secondary/15'>
                        <td colSpan={5} className='px-3 py-3'>
                          {item.definition === null ? (
                            <p className='text-sm text-warn'>
                              No versioned strategy definition is persisted for this sleeve.
                            </p>
                          ) : (
                            <>
                              <dl className='grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-3'>
                                <Detail
                                  label='Strategy ID'
                                  value={displayValue(definition.strategy_id)}
                                />
                                <Detail
                                  label='Implementation'
                                  value={displayValue(definition.implementation_name)}
                                />
                                <Detail
                                  label='Decision schedule'
                                  value={`${displayValue(
                                    definition.decision_frequency
                                  )} at ${displayValue(definition.decision_time)}`}
                                />
                                <Detail
                                  label='Benchmark reference'
                                  value={displayValue(definition.benchmark_symbol_or_sleeve)}
                                />
                                <Detail
                                  label='Universe'
                                  value={displayValue(definition.universe_definition)}
                                />
                                <Detail
                                  label='Data requirements'
                                  value={
                                    requirements.length > 0
                                      ? requirements.map(displayValue).join(', ')
                                      : 'None recorded'
                                  }
                                />
                                <Detail
                                  label='Constraints'
                                  value={`Long only: ${displayValue(
                                    definition.long_only
                                  )} · Leverage allowed: ${displayValue(
                                    definition.leverage_allowed
                                  )}`}
                                />
                                <Detail
                                  label='Sleeve id'
                                  value={<ShortId value={item.sleeve_id} chars={12} />}
                                />
                                <Detail
                                  label='Configuration hash'
                                  value={<ShortId value={item.configuration_hash} chars={16} />}
                                />
                              </dl>

                              <div className='mt-3 border-t border-border/40 pt-3'>
                                <p className='text-xs font-medium text-muted-foreground'>
                                  Parameters
                                </p>
                                {Object.keys(parameters).length === 0 ? (
                                  <p className='mt-1 text-xs text-muted-foreground'>
                                    No parameters recorded.
                                  </p>
                                ) : (
                                  <dl className='mt-1.5 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2 lg:grid-cols-3'>
                                    {Object.entries(parameters).map(([key, value]) => (
                                      <div key={key} className='flex flex-wrap gap-x-2'>
                                        <dt className='text-muted-foreground'>{humanize(key)}</dt>
                                        <dd className='tnum break-words text-foreground'>
                                          {displayValue(value)}
                                        </dd>
                                      </div>
                                    ))}
                                  </dl>
                                )}
                              </div>
                            </>
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
      )}
    </PanelCard>
  );
}

function Detail({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className='min-w-0'>
      <dt className='text-muted-foreground'>{label}</dt>
      <dd className='mt-0.5 break-words text-foreground'>{value}</dd>
    </div>
  );
}
