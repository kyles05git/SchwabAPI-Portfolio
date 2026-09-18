import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer, Legend } from "recharts";
import type { PositionRow } from "@/lib/types";
import { n } from "@/lib/format";

interface PortfolioDonutProps {
  positions: PositionRow[];
}

const COLORS = [
  "#0ea5e9", // sky
  "#8b5cf6", // violet
  "#22c55e", // green
  "#f59e0b", // amber
  "#ef4444", // red
  "#06b6d4", // cyan
  "#ec4899", // pink
  "#14b8a6", // teal
  "#f97316", // orange
  "#6366f1", // indigo
];

export function PortfolioDonut({ positions }: PortfolioDonutProps) {
  const data = positions
    .map((p) => ({
      name: p.symbol,
      value: n(p.market_value) ?? 0,
    }))
    .filter((d) => d.value > 0)
    .sort((a, b) => b.value - a.value);

  const total = data.reduce((sum, d) => sum + d.value, 0);

  if (data.length === 0) {
    return <p className="text-sm text-muted-foreground">No position data.</p>;
  }

  return (
    <div className="h-[220px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={data}
            cx="50%"
            cy="50%"
            innerRadius={55}
            outerRadius={85}
            paddingAngle={2}
            dataKey="value"
            stroke="hsl(222 25% 9%)"
            strokeWidth={2}
          >
            {data.map((_, index) => (
              <Cell key={index} fill={COLORS[index % COLORS.length]} />
            ))}
          </Pie>
          <Tooltip
            contentStyle={{
              background: "hsl(222 25% 9%)",
              border: "1px solid hsl(222 20% 14%)",
              borderRadius: "6px",
              fontSize: "12px",
              color: "hsl(210 20% 92%)",
            }}
            formatter={(value: number, name: string) => [
              `$${value.toLocaleString()} (${((value / total) * 100).toFixed(1)}%)`,
              name,
            ]}
          />
          <Legend
            layout="vertical"
            align="right"
            verticalAlign="middle"
            iconType="circle"
            iconSize={8}
            formatter={(value: string) => (
              <span className="text-xs text-muted-foreground">{value}</span>
            )}
          />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}