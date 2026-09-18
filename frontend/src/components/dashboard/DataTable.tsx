import type { ReactNode } from "react";

interface DataTableProps {
  headers: string[];
  rows: ReactNode[][];
  emptyMessage?: string;
}

export function DataTable({ headers, rows, emptyMessage }: DataTableProps) {
  if (rows.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        {emptyMessage ?? "No data."}
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="tnum w-full text-sm">
        <thead>
          <tr>
            {headers.map((h, i) => (
              <th
                key={h}
                className={`whitespace-nowrap px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground ${
                  i < 2 ? "text-left" : "text-right"
                }`}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((cells, r) => (
            <tr
              key={r}
              className="border-t border-border/40 transition-colors hover:bg-accent/30"
            >
              {cells.map((cell, i) => (
                <td
                  key={i}
                  className={`whitespace-nowrap px-3 py-2.5 ${
                    i < 2 ? "text-left" : "text-right"
                  }`}
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}