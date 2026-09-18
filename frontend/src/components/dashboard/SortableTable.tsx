import { Fragment, useState, useMemo, type ReactNode } from "react";
import { ChevronUp, ChevronDown, ChevronsUpDown, Search } from "lucide-react";

export interface ColumnDef<T> {
  key: string;
  header: string;
  align?: "left" | "right";
  sortable?: boolean;
  sortValue?: (row: T) => number | string;
  render: (row: T) => ReactNode;
}

interface SortableTableProps<T> {
  columns: ColumnDef<T>[];
  data: T[];
  emptyMessage?: string;
  searchable?: boolean;
  searchPlaceholder?: string;
  searchFilter?: (row: T, query: string) => boolean;
  onRowClick?: (row: T, index: number) => void;
  expandedIndex?: number | null;
  renderExpanded?: (row: T) => ReactNode;
}

type SortDir = "asc" | "desc" | null;

export function SortableTable<T>({
  columns,
  data,
  emptyMessage = "No data.",
  searchable = false,
  searchPlaceholder = "Filter…",
  searchFilter,
  onRowClick,
  expandedIndex,
  renderExpanded,
}: SortableTableProps<T>) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>(null);
  const [search, setSearch] = useState("");

  const handleSort = (key: string) => {
    if (sortKey === key) {
      if (sortDir === "asc") setSortDir("desc");
      else if (sortDir === "desc") {
        setSortKey(null);
        setSortDir(null);
      }
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  };

  const filteredData = useMemo(() => {
    if (!search || !searchFilter) return data;
    return data.filter((row) => searchFilter(row, search.toLowerCase()));
  }, [data, search, searchFilter]);

  const sortedData = useMemo(() => {
    if (!sortKey || !sortDir) return filteredData;
    const col = columns.find((c) => c.key === sortKey);
    if (!col?.sortValue) return filteredData;

    return [...filteredData].sort((a, b) => {
      const aVal = col.sortValue!(a);
      const bVal = col.sortValue!(b);
      let cmp = 0;
      if (typeof aVal === "number" && typeof bVal === "number") {
        cmp = aVal - bVal;
      } else {
        cmp = String(aVal).localeCompare(String(bVal));
      }
      return sortDir === "desc" ? -cmp : cmp;
    });
  }, [filteredData, sortKey, sortDir, columns]);

  return (
    <div>
      {searchable && (
        <div className="mb-3 relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={searchPlaceholder}
            className="w-full rounded-md border border-border bg-secondary/50 py-2 pl-9 pr-3 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/50"
          />
        </div>
      )}

      {sortedData.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">
          {emptyMessage}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="tnum w-full text-sm">
            <thead>
              <tr>
                {columns.map((col) => (
                  <th
                    key={col.key}
                    className={`whitespace-nowrap px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground ${
                      col.align === "right" ? "text-right" : "text-left"
                    } ${col.sortable ? "cursor-pointer select-none hover:text-foreground transition-colors" : ""}`}
                    onClick={col.sortable ? () => handleSort(col.key) : undefined}
                  >
                    <span className="inline-flex items-center gap-1">
                      {col.header}
                      {col.sortable && (
                        <span className="inline-flex flex-col">
                          {sortKey === col.key && sortDir === "asc" ? (
                            <ChevronUp className="h-3 w-3 text-primary" />
                          ) : sortKey === col.key && sortDir === "desc" ? (
                            <ChevronDown className="h-3 w-3 text-primary" />
                          ) : (
                            <ChevronsUpDown className="h-3 w-3 opacity-40" />
                          )}
                        </span>
                      )}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sortedData.map((row, r) => (
                <Fragment key={r}>
                  <tr
                    key={r}
                    className={`border-t border-border/40 transition-colors hover:bg-accent/30 ${
                      onRowClick ? "cursor-pointer" : ""
                    } ${expandedIndex === r ? "bg-accent/20" : ""}`}
                    onClick={() => onRowClick?.(row, r)}
                  >
                    {columns.map((col) => (
                      <td
                        key={col.key}
                        className={`whitespace-nowrap px-3 py-2.5 ${
                          col.align === "right" ? "text-right" : "text-left"
                        }`}
                      >
                        {col.render(row)}
                      </td>
                    ))}
                  </tr>
                  {expandedIndex === r && renderExpanded && (
                    <tr key={`${r}-expanded`} className="border-t border-border/20">
                      <td colSpan={columns.length} className="p-0">
                        {renderExpanded(row)}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}