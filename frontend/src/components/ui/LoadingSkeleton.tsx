interface LoadingSkeletonProps {
  error: string | null;
}

export function LoadingSkeleton({ error }: LoadingSkeletonProps) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <div className="flex flex-col items-center gap-5 px-6 text-center">
        {/* Loading indicator */}
        <div className="relative">
          <div className="h-10 w-10 rounded-full border-2 border-border" />
          <div className="absolute inset-0 h-10 w-10 animate-spin rounded-full border-2 border-transparent border-t-primary" />
        </div>

        {error ? (
          <div className="space-y-2">
            <p className="text-sm font-medium text-foreground">
              Unable to reach the dashboard server
            </p>
            <p className="text-xs text-muted-foreground max-w-sm">
              The server may be starting up or temporarily unavailable. The dashboard
              will reconnect automatically.
            </p>
            <details className="mt-3">
              <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground transition-colors">
                Technical details
              </summary>
              <p className="mt-2 rounded-md border border-border bg-card px-3 py-2 font-mono text-xs text-muted-foreground">
                {error}
              </p>
            </details>
          </div>
        ) : (
          <div className="space-y-1">
            <p className="text-sm font-medium text-foreground">Loading dashboard</p>
            <p className="text-xs text-muted-foreground">
              Connecting to the trading server…
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
