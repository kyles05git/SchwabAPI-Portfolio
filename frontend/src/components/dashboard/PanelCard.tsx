import type { ReactNode } from "react";

interface PanelCardProps {
  title: string;
  subtitle?: string;
  icon?: ReactNode;
  children: ReactNode;
}

export function PanelCard({ title, subtitle, icon, children }: PanelCardProps) {
  return (
    <section className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border/60 px-4 py-3">
        {icon && <span className="text-primary">{icon}</span>}
        <h2 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {title}
        </h2>
        {subtitle && (
          <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
            {subtitle}
          </span>
        )}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}