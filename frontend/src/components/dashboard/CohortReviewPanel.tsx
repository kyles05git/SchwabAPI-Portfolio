import { ClipboardCheck } from 'lucide-react';
import type {
  AccountingCheckView,
  CohortReviewView,
  OperatorActionName,
  OperatorDecisionView,
} from '@/lib/types';
import { PanelCard } from './PanelCard';
import {
  EmptyState,
  Metric,
  ShortId,
  StatusBadge,
  TechnicalDetails,
  dateLabel,
  dateTimeLabel,
  humanizeTitle,
} from './CohortStatus';

interface Props {
  review: CohortReviewView;
}

const ACTION_LABELS: Record<OperatorActionName, string> = {
  keep: 'Keep',
  modify: 'Modify',
  pause: 'Pause',
  retire: 'Retire',
};

/** A sleeve's display name, falling back to its stable id when no name is known. */
function sleeveLabel(name: string, id: string): string {
  return name || id;
}

function DifferenceRow({ item }: { item: AccountingCheckView }) {
  return (
    <li className='py-2.5'>
      <div className='flex flex-wrap items-baseline gap-x-2 gap-y-1'>
        <StatusBadge
          status={item.explained ? 'complete' : 'missing'}
          label={item.explained ? 'Explained' : 'Unexplained'}
        />
        <span className='text-sm font-medium text-foreground'>
          {sleeveLabel(item.sleeve_name, item.sleeve_id)}
        </span>
        <span className='text-xs text-muted-foreground'>
          {humanizeTitle(item.area)} · {dateLabel(item.session_date)}
        </span>
        {item.revision > 0 && (
          <span className='text-xs text-muted-foreground'>revision {item.revision}</span>
        )}
      </div>
      {item.summary && (
        <p className='mt-1 text-sm leading-5 text-foreground'>{item.summary}</p>
      )}
      {item.explanation ? (
        <p className='mt-1 text-sm leading-5 text-muted-foreground'>{item.explanation}</p>
      ) : (
        <p className='mt-1 text-sm leading-5 text-loss'>
          No explanation is recorded, so the operational review still fails on this.
        </p>
      )}
      <p className='mt-1 text-xs text-muted-foreground'>
        Recorded {dateTimeLabel(item.recorded_at)} by {item.recorded_by}
      </p>
    </li>
  );
}

function DecisionRow({ item, superseded }: { item: OperatorDecisionView; superseded?: boolean }) {
  return (
    <li className='py-2.5'>
      <div className='flex flex-wrap items-baseline gap-x-2 gap-y-1'>
        <StatusBadge status={superseded ? 'closed-session' : 'complete'} label={ACTION_LABELS[item.action]} />
        <span className='text-sm font-medium text-foreground'>
          {sleeveLabel(item.sleeve_name, item.sleeve_id)}
        </span>
        <span className='text-xs text-muted-foreground'>
          revision {item.revision}
          {superseded ? ' · superseded' : ''}
        </span>
      </div>
      <p className='mt-1 text-sm leading-5 text-muted-foreground'>{item.rationale}</p>
      <p className='mt-1 text-xs text-muted-foreground'>
        Recorded {dateTimeLabel(item.recorded_at)} by {item.recorded_by} ·{' '}
        <ShortId value={item.decision_id} />
      </p>
    </li>
  );
}

/**
 * The durable 30-session accounting review, read-only.
 *
 * Four states are kept visually distinct because conflating them is what would let an
 * operator trust a review that never happened: the store could not be read, nothing has
 * been recorded yet, the review is under way with observations still pending, and a
 * difference is recorded with no explanation. Only the last two are actionable, and only
 * the unexplained difference is shown in the loss colour.
 *
 * Nothing here writes. `schwab-trader cohort review` is the only write path, and a
 * recorded decision is a research disposition — it never authorizes live trading.
 */
export function CohortReviewPanel({ review }: Props) {
  if (!review.available) {
    return (
      <PanelCard title='Accounting review and decisions' icon={<ClipboardCheck className='h-3.5 w-3.5' />}>
        <EmptyState tone='pending'>
          {review.message ?? 'The durable cohort review could not be read.'}
        </EmptyState>
      </PanelCard>
    );
  }

  const nothingRecorded =
    review.recorded_check_count === 0 &&
    review.decisions.length === 0 &&
    review.notes.length === 0;
  const undecided = Math.max(0, review.member_count - review.decided_sleeve_count);

  return (
    <PanelCard
      title='Accounting review and decisions'
      subtitle='Read-only'
      icon={<ClipboardCheck className='h-3.5 w-3.5' />}
    >
      <div className='grid gap-4 sm:grid-cols-2 lg:grid-cols-4'>
        <Metric
          label='Observations reviewed'
          value={`${review.reviewed_observations} / ${review.official_observations}`}
          detail={`${review.recorded_check_count} accounting entries recorded`}
          emphasis
        />
        <Metric
          label='Unexplained differences'
          value={review.unexplained_difference_count}
          detail={`${review.differences.length} difference${
            review.differences.length === 1 ? '' : 's'
          } recorded in total`}
          emphasis
        />
        <Metric
          label='Sleeves decided'
          value={`${review.decided_sleeve_count} / ${review.member_count}`}
          detail={undecided > 0 ? `${undecided} awaiting a decision` : 'Every member decided'}
          emphasis
        />
        <Metric
          label='Review due'
          value={review.review_due ? 'Yes' : 'Not yet'}
          detail={`${review.completed_due_sessions} of ${review.review_target} completed due sessions`}
          emphasis
        />
      </div>

      {nothingRecorded ? (
        <div className='mt-4'>
          <EmptyState tone={review.review_due ? 'pending' : 'neutral'}>
            {review.message ??
              'No accounting review or operator decision has been recorded yet.'}{' '}
            {review.review_due
              ? 'The review is due; record it with `schwab-trader cohort review`.'
              : 'Nothing is owed until the review comes due.'}
          </EmptyState>
        </div>
      ) : null}

      {review.differences.length > 0 && (
        <div className='mt-4'>
          <h3 className='text-xs font-semibold uppercase tracking-wide text-muted-foreground'>
            Accounting differences
          </h3>
          <ul className='mt-1 divide-y divide-border/40'>
            {review.differences.map((item) => (
              <DifferenceRow key={item.entry_id} item={item} />
            ))}
          </ul>
        </div>
      )}

      {review.decisions.length > 0 && (
        <div className='mt-4'>
          <h3 className='text-xs font-semibold uppercase tracking-wide text-muted-foreground'>
            Current decisions
          </h3>
          <ul className='mt-1 divide-y divide-border/40'>
            {review.decisions.map((item) => (
              <DecisionRow key={item.decision_id} item={item} />
            ))}
          </ul>
        </div>
      )}

      {review.notes.length > 0 && (
        <TechnicalDetails className='mt-4' label={`Operator notes (${review.notes.length})`}>
          <ul className='space-y-2'>
            {review.notes.map((item) => (
              <li key={item.note_id} className='text-sm text-muted-foreground'>
                <span className='text-foreground'>{item.note}</span>
                <span className='block text-xs'>
                  {item.sleeve_id ? `${sleeveLabel(item.sleeve_name, item.sleeve_id)} · ` : ''}
                  {dateTimeLabel(item.recorded_at)} by {item.recorded_by}
                </span>
              </li>
            ))}
          </ul>
        </TechnicalDetails>
      )}

      {review.pending_observations.length > 0 && (
        <TechnicalDetails
          className='mt-2'
          label={`Observations still to review (${review.pending_observations.length})`}
        >
          <ul className='space-y-1 text-xs text-muted-foreground'>
            {review.pending_observations.slice(0, 50).map((item) => (
              <li key={item.observation_key}>
                {dateLabel(item.session_date)} · {sleeveLabel(item.sleeve_name, item.sleeve_id)} ·
                missing {item.missing_areas.join(', ')}
              </li>
            ))}
            {review.pending_observations.length > 50 && (
              <li>… and {review.pending_observations.length - 50} more.</li>
            )}
          </ul>
        </TechnicalDetails>
      )}

      {(review.superseded_decisions.length > 0 || review.superseded_checks.length > 0) && (
        <TechnicalDetails
          className='mt-2'
          label={`Superseded records (${
            review.superseded_decisions.length + review.superseded_checks.length
          })`}
        >
          <p className='mb-2 text-xs text-muted-foreground'>
            Corrections never overwrite. Each entry below is preserved exactly as it was
            first recorded.
          </p>
          {review.superseded_decisions.length > 0 && (
            <ul className='divide-y divide-border/40'>
              {review.superseded_decisions.map((item) => (
                <DecisionRow key={item.decision_id} item={item} superseded />
              ))}
            </ul>
          )}
          {review.superseded_checks.length > 0 && (
            <ul className='mt-2 divide-y divide-border/40'>
              {review.superseded_checks.map((item) => (
                <DifferenceRow key={item.entry_id} item={item} />
              ))}
            </ul>
          )}
        </TechnicalDetails>
      )}

      <p className='mt-4 text-xs leading-5 text-muted-foreground'>
        This panel is read-only; records are written with{' '}
        <code className='font-mono'>schwab-trader cohort review</code>. A decision is a
        research disposition about a paper experiment — it does not promote, pause,
        retire, or reconfigure a sleeve, and it never authorizes live trading.
      </p>
    </PanelCard>
  );
}
