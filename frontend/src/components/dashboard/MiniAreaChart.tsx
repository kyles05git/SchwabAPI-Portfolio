import { AreaChart, Area, Tooltip, ResponsiveContainer } from "recharts";
import type { Num } from "@/lib/types";
import { n } from "@/lib/format";

interface MiniAreaChartProps {
  points: Num[];
  width?: number;
  height?: number;
  name?: string;
}

export function MiniAreaChart({ points, height = 40, name = "Value" }: MiniAreaChartProps) {
  const values = points.map((p) => n(p) ?? 0);
  if (values.length < 2) {
    return <span className="text-muted-foreground">—</span>;
  }

  const data = values.map((v, i) => ({ idx: i, value: v }));
  const up = values[values.length - 1] >= values[0];
  const color = up ? "#22c55e" : "#ef4444";

  return (
    <div style={{ width: "100%", height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
          <defs>
            <linearGradient id={`grad-${name}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.3} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Tooltip
            contentStyle={{
              background: "hsl(222 25% 9%)",
              border: "1px solid hsl(222 20% 14%)",
              borderRadius: "6px",
              fontSize: "11px",
              color: "hsl(210 20% 92%)",
            }}
            formatter={(value: number) => [`$${value.toLocaleString()}`, name]}
            labelFormatter={(label: number) => `Period ${label + 1}`}
          />
          <Area
            type="monotone"
            dataKey="value"
            stroke={color}
            strokeWidth={1.5}
            fill={`url(#grad-${name})`}
            dot={false}
            activeDot={{ r: 3, fill: color, stroke: "hsl(222 25% 9%)", strokeWidth: 2 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}