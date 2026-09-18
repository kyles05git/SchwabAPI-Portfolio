import { useState } from 'react';
import { ShieldAlert, ShieldCheck } from 'lucide-react';
import type { SafetyView } from '@/lib/types';

/**
 * Kill-switch status and control, pinned below the header on every view.
 *
 * This is the dashboard's only state-changing action, and it can only *halt* trading
 * — resuming stays on the CLI. It is deliberately reachable from every view so an
 * operator never has to navigate to stop the agent.
 */
export function KillSwitchBar({
  view,
  onKilled,
}: {
  view: SafetyView;
  onKilled: () => void;
}) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  async function engage() {
    setBusy(true);
    try {
      await fetch('/kill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ reason }),
      });
      setOpen(false);
      setReason('');
      onKilled();
    } finally {
      setBusy(false);
    }
  }

  if (view.kill_engaged) {
    return (
      <div className='border-b border-destructive/30 bg-destructive/10 px-5 py-2.5'>
        <div className='mx-auto flex max-w-[1520px] flex-wrap items-center gap-x-3 gap-y-1'>
          <ShieldAlert className='h-4 w-4 shrink-0 text-destructive' />
          <span className='text-sm font-semibold text-destructive'>
            Kill switch engaged — autonomous trading is halted
          </span>
          {view.kill_since && (
            <span className='text-xs text-destructive/80'>
              since {new Date(view.kill_since).toLocaleString()}
            </span>
          )}
          {view.kill_reason && (
            <span className='text-xs text-destructive/80'>· {view.kill_reason}</span>
          )}
          <code className='ml-auto rounded bg-background/60 px-1.5 py-0.5 font-mono text-xs text-destructive'>
            schwab-trader safety resume
          </code>
        </div>
      </div>
    );
  }

  return (
    <div className='border-b border-border/60 bg-secondary/20 px-5 py-2'>
      <div className='mx-auto flex max-w-[1520px] flex-wrap items-center gap-x-3 gap-y-2'>
        <ShieldCheck className='h-4 w-4 shrink-0 text-profit' />
        <span className='text-sm text-foreground'>
          Kill switch <span className='font-semibold text-profit'>clear</span>
        </span>
        <span className='text-xs text-muted-foreground'>
          {view.trades_today} trades today
        </span>

        <div className='ml-auto flex flex-wrap items-center gap-2'>
          {open && (
            <input
              type='text'
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder='reason (optional)'
              maxLength={120}
              aria-label='Kill-switch reason'
              className='w-56 rounded-md border border-input bg-background px-2.5 py-1 text-sm text-foreground placeholder:text-muted-foreground focus:border-ring focus:outline-none focus:ring-1 focus:ring-ring'
            />
          )}
          <button
            type='button'
            onClick={() => (open ? void engage() : setOpen(true))}
            disabled={busy}
            className='cursor-pointer rounded-md border border-destructive/40 bg-destructive/10 px-3 py-1 text-sm font-semibold text-destructive transition-colors hover:bg-destructive/20 focus:outline-none focus-visible:ring-1 focus-visible:ring-destructive disabled:cursor-not-allowed disabled:opacity-50'
          >
            {open ? 'Confirm halt' : 'Engage kill switch'}
          </button>
          {open && (
            <button
              type='button'
              onClick={() => setOpen(false)}
              className='cursor-pointer rounded-md px-2 py-1 text-sm text-muted-foreground transition-colors hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-primary/60'
            >
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
