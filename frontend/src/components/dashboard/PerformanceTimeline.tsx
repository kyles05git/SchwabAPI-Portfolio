import { useState, useMemo } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import type { EquitySeries } from "@/lib/types";
import { n } from "@/lib/format";
import { PanelCard } from "./PanelCard";
import { TrendingUp } from "lucide-react";

interface PerformanceTimelineProps {
  curves: EquitySeries[];
  benchmark: string;
}

/**
 * The equity-series contract contains ordered numeric points but no timestamps.
 * Range controls use observation counts, not calendar durations.
 */
const RANGE_OPTIONS = [
  { label: "7 obs", count: 7 },
  { label: "30 obs", count: 30 },
  { label: "90 obs", count: 90 },
  { label: "All", count: 0 },
] as const;

const CURVE_COLORS = [
  "hsl(var(--chart-1))",
  "hsl(var(--chart-4))",
  "hsl(var(--chart-2))",
  "hsl(var(--chart-3))",
  "hsl(var(--chart-5))",
  "hsl(var(--chart-6))",
];

export function PerformanceTimeline({ curves, benchmark }: PerformanceTimelineProps) {
  const [range, setRange] = useState<string>("All");

  const chartData = useMemo(() => {
    if (curves.length === 0) return [];

    const maxLen = Math.max(...curves.map((c) => c.points.length));
    const rangeOpt = RANGE_OPTIONS.find((r) => r.label === range);
    const sliceStart = rangeOpt && rangeOpt.count > 0 ? Math.max(0, maxLen - rangeOpt.count) : 0;

    const data: Record<string, number | string>[] = [];
    for (let i = sliceStart; i < maxLen; i++) {
      const point: Record<string, number | string> = { observation: i - sliceStart + 1 };
      for (const curve of curves) {
        const val = n(curve.points[i]);
        if (val !== null) {
          point[curve.name] = val;
        }
      }
      data.push(point);
    }
    return data;
  }, [curves, range]);

  const sortedCurves = useMemo(
    () => [...curves].sort((a, b) => (a.name === benchmark ? 1 : b.name === benchmark ? -1 : 0)),
    [curves, benchmark]
  );

  if (curves.length === 0) {
    return (
      <PanelCard
        title="Performance Timeline"
        subtitle="Equity Curves"
        icon={<TrendingUp className="h-3.5 w-3.5" />}
      >
        <p className="py-8 text-center text-sm text-muted-foreground">
          No equity curve data available.
        </p>
      </PanelCard>
    );
  }

  return (
    <PanelCard
      title="Performance Timeline"
      subtitle="Equity Curves"
      icon={<TrendingUp className="h-3.5 w-3.5" />}
    >
      {/* Range selector — observation-based, not calendar */}
      <div className="mb-4 flex items-center gap-1" role="group" aria-label="Observation range">
        {RANGE_OPTIONS.map((opt) => (
          <button
            key={opt.label}
            type="button"
            onClick={() => setRange(opt.label)}
            aria-pressed={range === opt.label}
            className={`focus-ring cursor-pointer rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors ${
              range === opt.label
                ? "bg-primary/15 text-primary"
                : "text-muted-foreground hover:text-foreground hover:bg-accent/50"
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* Chart */}
      <div className="h-[220px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={chartData} margin={{ top: 5, right: 10, bottom: 5, left: 10 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis
              dataKey="observation"
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              axisLine={{ stroke: "hsl(var(--border))" }}
              tickLine={false}
              label={{ value: "Observation", position: "insideBottom", offset: -2, fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            />
            <YAxis
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              axisLine={{ stroke: "hsl(var(--border))" }}
              tickLine={false}
              tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
            />
            <Tooltip
              contentStyle={{
                background: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                borderRadius: "6px",
                fontSize: "11px",
                color: "hsl(var(--foreground))",
              }}
              formatter={(value: number, name: string) => [
                `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
                name === benchmark ? `${name} (benchmark)` : name,
              ]}
              labelFormatter={(label) => `Observation ${label}`}
            />
            {sortedCurves.map((curve, i) => (
              <Area
                key={curve.sleeve_id}
                type="monotone"
                dataKey={curve.name}
                stroke={CURVE_COLORS[i % CURVE_COLORS.length]}
                strokeWidth={curve.name === benchmark ? 1.5 : 2}
                strokeDasharray={curve.name === benchmark ? "4 2" : undefined}
                fill="none"
                dot={false}
                activeDot={{ r: 3 }}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Legend */}
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5 border-t border-border/40 pt-3">
        {sortedCurves.map((curve, i) => (
          <span key={curve.sleeve_id} className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span
              className="inline-block h-2 w-2 rounded-full shrink-0"
              style={{ backgroundColor: CURVE_COLORS[i % CURVE_COLORS.length] }}
            />
            <span className="truncate max-w-32">{curve.name}</span>
            {curve.name === benchmark && (
              <span className="text-[10px] opacity-60">(bench)</span>
            )}
          </span>
        ))}
      </div>
    </PanelCard>
  );
}
