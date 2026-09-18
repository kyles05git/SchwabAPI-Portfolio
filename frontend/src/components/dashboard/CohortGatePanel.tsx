import { ShieldCheck } from 'lucide-react';
import type { CohortPhaseView, GateRuleView, OperationalGateView, RulePresentation } from '@/lib/types';
import { PanelCard } from './PanelCard';
import { EmptyState, PhaseBadge, TechnicalDetails, presentationLabel } from './CohortStatus';

interface Props {
  gate: OperationalGateView;
  phase: CohortPhaseView | null;
}

const GROUP_ORDER: RulePresentation[] = ['needs-attention', 'awaiting-evidence', 'healthy'];

const GROUP_STYLE: Record<RulePresentation, { dot: string; text: string }> = {
  // Red is reserved for genuinely actionable failures.
  'needs-attention': { dot: 'bg-loss', text: 'text-loss' },
  'awaiting-evidence': { dot: 'bg-warn', text: 'text-warn' },
  healthy: { dot: 'bg-profit', text: 'text-profit' },
};

function RuleRow({ rule }: { rule: GateRuleView }) {
  const style = GROUP_STYLE[rule.presentation];
  return (
    <li className='py-2'>
      <div className='flex flex-wrap items-baseline gap-x-2 gap-y-1'>
        <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} aria-hidden />
        <span className='text-sm font-medium text-foreground'>{rule.label || rule.rule}</span>
        <span className={`text-xs ${style.text}`}>{presentationLabel(rule.presentation)}</span>
      </div>
      <p className='mt-1 pl-3.5 text-sm leading-5 text-muted-foreground'>{rule.reason}</p>
      {rule.evidence.length > 0 && (
        <TechnicalDetails
          className='mt-1.5 pl-3.5'
          label={`Evidence (${rule.evidence.length})`}
        >
          <ul className='list-disc space-y-1 pl-5 text-xs text-muted-foreground'>
            {rule.evidence.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </TechnicalDetails>
      )}
    </li>
  );
}

/**
 * The 30-session operational review.
 *
 * Requirements are grouped by whether they are actionable now, still awaiting
 * evidence, or healthy. Only actionable failures are open by default. The underlying
 * gate is unchanged and still fails closed for authorization.
 */
export function CohortGatePanel({ gate, phase }: Props) {
  if (!gate.available) {
    return (
      <PanelCard
        title='30-session operational review'
        icon={<ShieldCheck className='h-3.5 w-3.5' />}
      >
        <EmptyState tone='pending'>
          {gate.message ?? 'Operational-review evidence is unavailable.'}
        </EmptyState>
      </PanelCard>
    );
  }

  const grouped = new Map<RulePresentation, GateRuleView[]>(
    GROUP_ORDER.map((key) => [key, []])
  );
  for (const rule of gate.rules) {
    grouped.get(rule.presentation)?.push(rule);
  }
  const attention = grouped.get('needs-attention') ?? [];
  const awaiting = grouped.get('awaiting-evidence') ?? [];
  const healthy = grouped.get('healthy') ?? [];

  return (
    <PanelCard
      title='30-session operational review'
      icon={<ShieldCheck className='h-3.5 w-3.5' />}
    >
      <div className='flex flex-wrap items-center gap-x-5 gap-y-2'>
        {phase && <PhaseBadge phase={phase.phase} />}
        {phase && (
          <span className='tnum text-sm text-foreground'>
            {phase.completed_due_sessions} of {phase.review_target} sessions
          </span>
        )}
        <span className='text-sm text-profit'>{healthy.length} passed</span>
        <span className='text-sm text-warn'>{awaiting.length} awaiting evidence</span>
        <span className={`text-sm ${attention.length > 0 ? 'text-loss' : 'text-muted-foreground'}`}>
          {attention.length} need attention
        </span>
      </div>

      <p className='mt-3 text-xs leading-5 text-muted-foreground'>
        This review measures whether the experiment is operationally sound. It never
        assesses investment alpha
        {gate.investment_alpha_assessed ? '' : ' (not assessed)'} and never authorizes
        live trading
        {gate.live_trading_authorized ? '' : ' (not authorized)'}.
      </p>

      {attention.length > 0 ? (
        <div className='mt-4'>
          <h3 className='text-xs font-semibold uppercase tracking-wide text-loss'>
            Needs attention
          </h3>
          <ul className='mt-1 divide-y divide-border/40'>
            {attention.map((rule) => (
              <RuleRow key={rule.rule} rule={rule} />
            ))}
          </ul>
        </div>
      ) : (
        <div className='mt-4'>
          <EmptyState>
            {awaiting.length > 0
              ? 'No requirement has failed. The remaining requirements are awaiting evidence.'
              : 'Every operational requirement is currently satisfied.'}
          </EmptyState>
        </div>
      )}

      {awaiting.length > 0 && (
        <TechnicalDetails
          className='mt-4'
          label={`Awaiting evidence (${awaiting.length})`}
        >
          <ul className='divide-y divide-border/40'>
            {awaiting.map((rule) => (
              <RuleRow key={rule.rule} rule={rule} />
            ))}
          </ul>
        </TechnicalDetails>
      )}

      {healthy.length > 0 && (
        <TechnicalDetails className='mt-2' label={`Healthy (${healthy.length})`}>
          <ul className='divide-y divide-border/40'>
            {healthy.map((rule) => (
              <RuleRow key={rule.rule} rule={rule} />
            ))}
          </ul>
        </TechnicalDetails>
      )}

      <TechnicalDetails className='mt-3' label='Gate contract'>
        <dl className='grid gap-x-4 gap-y-1 text-xs sm:grid-cols-[13rem_1fr]'>
          <dt className='text-muted-foreground'>Gate status</dt>
          <dd className='text-foreground'>{gate.status ?? 'Unavailable'}</dd>
          <dt className='text-muted-foreground'>Operationally useful</dt>
          <dd className='text-foreground'>
            {gate.operationally_useful === null ? 'Unavailable' : String(gate.operationally_useful)}
          </dd>
          <dt className='text-muted-foreground'>Investment alpha assessed</dt>
          <dd className='text-foreground'>{String(gate.investment_alpha_assessed)}</dd>
          <dt className='text-muted-foreground'>Live trading authorized</dt>
          <dd className='text-foreground'>{String(gate.live_trading_authorized)}</dd>
          <dt className='text-muted-foreground'>Summary</dt>
          <dd className='text-foreground'>{gate.summary ?? 'Unavailable'}</dd>
        </dl>
      </TechnicalDetails>
    </PanelCard>
  );
}
