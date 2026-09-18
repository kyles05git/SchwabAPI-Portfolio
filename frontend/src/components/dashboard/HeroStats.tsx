import type { SummaryView } from "@/lib/types";
import { n } from "@/lib/format";
import { AnimatedValue } from "./AnimatedValue";
import { DollarSign, TrendingUp, Wallet, ShieldAlert } from "lucide-react";

interface HeroStatsProps {
  summary: SummaryView;
}

const formatMoney = (v: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(v);

const formatPL = (v: number) => `${v > 0 ? "+" : ""}${v.toFixed(2)}`;

export function HeroStats({ summary }: HeroStatsProps) {
  const plValue = n(summary.day_pl) ?? 0;
  const liqValue = n(summary.liquidation_value) ?? 0;
  const cashValue = n(summary.cash_available) ?? 0;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatCard
        label="Liquidation Value"
        icon={<DollarSign className="h-4 w-4" />}
      >
        <AnimatedValue value={liqValue} formatter={formatMoney} className="text-xl font-bold text-foreground" />
      </StatCard>
      <StatCard
        label="Day P/L"
        icon={<TrendingUp className="h-4 w-4" />}
      >
        <AnimatedValue
          value={plValue}
          formatter={formatPL}
          className={`text-xl font-bold ${plValue > 0 ? "text-profit" : plValue < 0 ? "text-loss" : "text-foreground"}`}
        />
      </StatCard>
      <StatCard
        label="Cash Available"
        icon={<Wallet className="h-4 w-4" />}
      >
        <AnimatedValue value={cashValue} formatter={formatMoney} className="text-xl font-bold text-foreground" />
      </StatCard>
      <StatCard
        label="Kill Switch"
        icon={<ShieldAlert className="h-4 w-4" />}
      >
        <p className={`text-xl font-bold ${summary.kill_engaged ? "text-loss" : "text-profit"}`}>
          {summary.kill_engaged ? "ENGAGED" : "Clear"}
        </p>
      </StatCard>
    </div>
  );
}

function StatCard({
  label,
  icon,
  children,
}: {
  label: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center gap-2 text-muted-foreground">
        {icon}
        <span className="text-[11px] font-medium uppercase tracking-wider">
          {label}
        </span>
      </div>
      <div className="mt-2">{children}</div>
    </div>
  );
}