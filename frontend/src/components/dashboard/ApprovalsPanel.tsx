import { useState } from "react";
import type { ApprovalsView } from "@/lib/types";
import { PanelCard } from "./PanelCard";
import { Clock, Copy, Check, Terminal } from "lucide-react";

interface Props {
  view: ApprovalsView;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access may be denied
    }
  }

  return (
    <button
      type="button"
      onClick={handleCopy}
      className="focus-ring inline-flex items-center gap-1 rounded p-1 text-muted-foreground transition-colors hover:text-foreground cursor-pointer"
      aria-label={copied ? "Copied" : "Copy to clipboard"}
      title={copied ? "Copied!" : "Copy"}
    >
      {copied ? <Check className="h-3 w-3 text-profit" /> : <Copy className="h-3 w-3" />}
    </button>
  );
}

export function ApprovalsPanel({ view }: Props) {
  return (
    <PanelCard
      title="Pending Approvals"
      subtitle={view.rows.length > 0 ? `${view.rows.length}` : undefined}
      icon={<Clock className="h-3.5 w-3.5" />}
    >
      {view.rows.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No pending approvals.
        </p>
      ) : (
        <div className="space-y-3">
          {view.rows.map((row) => {
            const cliCommand = `schwab-trader agent approve ${row.token}`;
            const expired = row.expires_in_min <= 0;

            return (
              <div
                key={row.token}
                className={`rounded-lg border p-3 ${
                  expired
                    ? "border-border bg-muted/30 opacity-60"
                    : "border-border bg-card"
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-foreground">{row.describe}</p>
                    {row.rationale && (
                      <p className="mt-1 text-xs text-muted-foreground">{row.rationale}</p>
                    )}
                    <p className="mt-1 text-xs text-muted-foreground">
                      Account: …{row.account_tail}
                    </p>
                  </div>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      expired
                        ? "bg-loss/10 text-loss"
                        : row.expires_in_min <= 5
                          ? "bg-warn/10 text-warn"
                          : "bg-secondary text-muted-foreground"
                    }`}
                  >
                    {expired ? "Expired" : `${row.expires_in_min}m`}
                  </span>
                </div>

                {/* Token with copy */}
                <div className="mt-2 flex items-center gap-1.5">
                  <code className="flex-1 truncate rounded bg-secondary px-2 py-1 font-mono text-[11px] text-primary">
                    {row.token}
                  </code>
                  <CopyButton text={row.token} />
                </div>

                {/* CLI command */}
                {!expired && (
                  <div className="mt-2 flex items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1.5">
                    <Terminal className="h-3 w-3 shrink-0 text-muted-foreground" />
                    <code className="flex-1 truncate font-mono text-[11px] text-muted-foreground">
                      {cliCommand}
                    </code>
                    <CopyButton text={cliCommand} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </PanelCard>
  );
}
