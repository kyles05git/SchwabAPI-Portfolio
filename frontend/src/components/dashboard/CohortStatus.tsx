import { useState, type ReactNode } from 'react';
import { Check, Copy } from 'lucide-react';
import type {
  CohortPhaseName,
  Num,
  RulePresentation,
  RunTiming,
  SessionTimingState,
} from '@/lib/types';
import { n } from '@/lib/format';

const POSITIVE = new Set([
  'ready',
  'official',
  'completed',
  'complete',
  'pass',
  'passed',
  'mature',
  'healthy',
  'executed',
  'settled',
]);
const CAUTION = new Set([
  'partial',
  'stale',
  'interrupted',
  'insufficient_history',
  'insufficient-history',
  'no_overlap',
  'in_progress',
  'pending',
  'scheduled',
  'collecting',
  'review-ready',
  'awaiting-evidence',
  'awaiting-execution',
  'awaiting-provider-data',
  // Nothing ran and nothing was lost: a retryable wait, not a failed session.
  'awaiting-data',
]);
const NEUTRAL = new Set(['upcoming', 'closed-session', 'no-session-scheduled']);
const NEGATIVE = new Set([
  'failed',
  'fail',
  'not_ready',
  'missing',
  'missed',
  'attention-needed',
  'needs-attention',
  'overdue',
]);

/**
 * Turn a stable identifier into readable text.
 *
 * Identifiers arrive in three shapes: `snake_case` reason codes, `kebab-case` gate
 * rules and phases, and `namespace:value` snapshot keys. All three are handled.
 */
export function humanize(value: string): string {
  return value.replaceAll(':', ' / ').replaceAll('_', ' ').replaceAll('-', ' ');
}

/** Sentence-case a humanized identifier, leaving existing capitals alone. */
export function humanizeTitle(value: string): string {
  const text = humanize(value);
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function toneFor(status: string | null): string {
  const normalized = status?.toLowerCase() ?? 'unavailable';
  if (NEUTRAL.has(normalized)) return 'border-border bg-secondary text-muted-foreground';
  if (POSITIVE.has(normalized)) return 'border-profit/30 bg-profit/10 text-profit';
  if (NEGATIVE.has(normalized)) return 'border-loss/30 bg-loss/10 text-loss';
  if (CAUTION.has(normalized)) return 'border-warn/30 bg-warn/10 text-warn';
  return 'border-border bg-secondary text-muted-foreground';
}

export function StatusBadge({
  status,
  label,
  size = 'sm',
}: {
  status: string | null;
  label?: string;
  size?: 'sm' | 'lg';
}) {
  const normalized = status?.toLowerCase() ?? 'unavailable';
  const sizing =
    size === 'lg' ? 'px-3 py-1 text-xs tracking-wide' : 'px-2 py-0.5 text-[11px] tracking-wide';

  return (
    <span
      className={`inline-flex items-center rounded-full border font-semibold ${sizing} ${toneFor(
        normalized
      )}`}
    >
      {label ?? humanizeTitle(normalized)}
    </span>
  );
}

const PHASE_LABELS: Record<CohortPhaseName, string> = {
  scheduled: 'Scheduled',
  collecting: 'Collecting',
  'review-ready': 'Review ready',
  passed: 'Passed',
  'attention-needed': 'Attention needed',
  failed: 'Failed',
};

/** The experiment's life-cycle phase. Never renders "failed" for an unstarted cohort. */
export function PhaseBadge({
  phase,
  size = 'lg',
}: {
  phase: CohortPhaseName;
  size?: 'sm' | 'lg';
}) {
  return <StatusBadge status={phase} label={PHASE_LABELS[phase]} size={size} />;
}

const TIMING_LABELS: Record<SessionTimingState, string> = {
  'no-session-scheduled': 'No session scheduled',
  upcoming: 'Upcoming',
  'awaiting-execution': 'Run due',
  'awaiting-provider-data': 'Awaiting provider data — retryable',
  overdue: 'Overdue',
  settled: 'Up to date',
};

/**
 * Where the clock is, not how the experiment is going.
 *
 * A pending run before its session's close is `Upcoming`; after the close but inside
 * the scheduler's grace period it is `Run due`. Neither is a failure, and neither is
 * rendered in the loss colour.
 */
export function TimingBadge({
  timing,
  size = 'sm',
}: {
  timing: SessionTimingState;
  size?: 'sm' | 'lg';
}) {
  return <StatusBadge status={timing} label={TIMING_LABELS[timing]} size={size} />;
}

const RUN_TIMING_LABELS: Record<RunTiming, string> = {
  upcoming: 'Upcoming',
  'awaiting-execution': 'Awaiting execution',
  'awaiting-provider-data': 'Awaiting provider data — retryable',
  overdue: 'Overdue',
  executed: 'Executed',
  'closed-session': 'Market closed',
};

export function runTimingLabel(timing: RunTiming): string {
  return RUN_TIMING_LABELS[timing] ?? humanizeTitle(timing);
}

const PRESENTATION_LABELS: Record<RulePresentation, string> = {
  healthy: 'Healthy',
  'awaiting-evidence': 'Awaiting evidence',
  'needs-attention': 'Needs attention',
};

export function presentationLabel(value: RulePresentation): string {
  return PRESENTATION_LABELS[value];
}

/**
 * A stable database id or configuration hash, shortened.
 *
 * Full 64-character identities are never a primary label; they stay behind this
 * control, which reveals the whole value on hover and offers a copy.
 */
export function ShortId({ value, chars = 8 }: { value: string | null; chars?: number }) {
  const [copied, setCopied] = useState(false);
  if (!value) return <span className='text-muted-foreground'>Unavailable</span>;

  const truncated = value.length > chars ? `${value.slice(0, chars)}…` : value;

  async function copy() {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be denied; the full value stays available in the title.
    }
  }

  return (
    <span className='inline-flex items-center gap-1'>
      <code className='font-mono text-[11px] text-muted-foreground' title={value}>
        {truncated}
      </code>
      <button
        type='button'
        onClick={copy}
        aria-label={copied ? 'Copied' : `Copy full identifier ${value}`}
        className='rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-primary/60'
      >
        {copied ? <Check className='h-3 w-3 text-profit' /> : <Copy className='h-3 w-3' />}
      </button>
    </span>
  );
}

/** Collapsed container for stable ids, hashes, and raw reason codes. */
export function TechnicalDetails({
  children,
  label = 'Technical details',
  className = '',
}: {
  children: ReactNode;
  label?: string;
  className?: string;
}) {
  return (
    <details className={`group ${className}`}>
      <summary className='inline-flex cursor-pointer items-center gap-1 rounded text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-1 focus-visible:ring-primary/60'>
        <span className='transition-transform group-open:rotate-90'>›</span>
        {label}
      </summary>
      <div className='mt-2'>{children}</div>
    </details>
  );
}

/** A labelled figure. `emphasis` gives summary numbers more weight than metadata. */
export function Metric({
  label,
  value,
  detail,
  emphasis = false,
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  emphasis?: boolean;
}) {
  return (
    <div className='min-w-0'>
      <p className='text-xs font-medium text-muted-foreground'>{label}</p>
      <div
        className={`mt-0.5 tnum font-semibold text-foreground ${
          emphasis ? 'text-xl' : 'text-sm'
        }`}
      >
        {value}
      </div>
      {detail && <div className='mt-0.5 text-xs leading-4 text-muted-foreground'>{detail}</div>}
    </div>
  );
}

/** One concise message used in place of an empty or not-yet-populated panel. */
export function EmptyState({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'neutral' | 'pending';
}) {
  const style =
    tone === 'pending'
      ? 'border-warn/25 bg-warn/5 text-warn'
      : 'border-border/60 bg-secondary/15 text-muted-foreground';
  return <p className={`rounded-md border px-3 py-2.5 text-sm ${style}`}>{children}</p>;
}

export function ratioPct(value: Num | null | undefined, signed = false): string {
  const parsed = n(value);
  if (parsed === null) return 'Unavailable';
  const percentage = parsed * 100;
  const sign = signed && percentage > 0 ? '+' : '';
  return `${sign}${percentage.toFixed(2)}%`;
}

export function directPct(value: Num | null | undefined): string {
  const parsed = n(value);
  return parsed === null ? 'Unavailable' : `${parsed.toFixed(2)}%`;
}

export function scalar(value: Num | null | undefined, digits = 2): string {
  const parsed = n(value);
  return parsed === null ? 'Unavailable' : parsed.toFixed(digits);
}

export function dateLabel(value: string | null | undefined): string {
  if (!value) return 'Unavailable';
  const parsed = new Date(`${value}T00:00:00`);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

export function longDateLabel(value: string | null | undefined): string {
  if (!value) return 'Unavailable';
  const parsed = new Date(`${value}T00:00:00`);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });
}

export function dateTimeLabel(value: string | null | undefined): string {
  if (!value) return 'Unavailable';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}
