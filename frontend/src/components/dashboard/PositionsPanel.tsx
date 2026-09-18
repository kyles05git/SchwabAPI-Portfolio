import { useState } from "react";
import type { PositionsView, PositionRow } from "@/lib/types";
import { money, qty, signClass, n } from "@/lib/format";
import { PanelCard } from "./PanelCard";
import { SortableTable, type ColumnDef } from "./SortableTable";
import { PortfolioDonut } from "./PortfolioDonut";
import { BarChart3, ChevronRight } from "lucide-react";

interface Props {
  view: PositionsView;
}

export function PositionsPanel({ view }: Props) {
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);

  const columns: ColumnDef<PositionRow>[] = [
    {
      key: "expand",
      header: "",
      align: "left",
      render: (_, ) => (
        <ChevronRight className="h-3.5 w-3.5 text-muted-foreground transition-transform" />
      ),
    },
    {
      key: "symbol",
      header: "Symbol",
      align: "left",
      sortable: true,
      sortValue: (row) => row.symbol,
      render: (row) => <span className="font-semibold text-foreground">{row.symbol}</span>,
    },
    {
      key: "quantity",
      header: "Qty",
      align: "right",
      sortable: true,
      sortValue: (row) => n(row.quantity) ?? 0,
      render: (row) => qty(row.quantity),
    },
    {
      key: "average_price",
      header: "Avg Price",
      align: "right",
      sortable: true,
      sortValue: (row) => n(row.average_price) ?? 0,
      render: (row) => <span className="text-muted-foreground">{money(row.average_price)}</span>,
    },
    {
      key: "market_value",
      header: "Mkt Value",
      align: "right",
      sortable: true,
      sortValue: (row) => n(row.market_value) ?? 0,
      render: (row) => money(row.market_value),
    },
    {
      key: "day_pl",
      header: "Day P/L",
      align: "right",
      sortable: true,
      sortValue: (row) => n(row.day_pl) ?? 0,
      render: (row) => <span className={signClass(row.day_pl)}>{money(row.day_pl)}</span>,
    },
  ];

  const handleRowClick = (_: PositionRow, index: number) => {
    setExpandedIndex(expandedIndex === index ? null : index);
  };

  const renderExpanded = (row: PositionRow) => {
    const avgPrice = n(row.average_price) ?? 0;
    const mktValue = n(row.market_value) ?? 0;
    const quantity = n(row.quantity) ?? 0;
    const currentPrice = quantity > 0 ? mktValue / quantity : 0;
    const totalCost = avgPrice * quantity;
    const totalGain = mktValue - totalCost;
    const totalGainPct = totalCost > 0 ? (totalGain / totalCost) * 100 : 0;

    return (
      <div className="bg-accent/10 px-6 py-4 animate-fade-in">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <DetailItem label="Current Price" value={`$${currentPrice.toFixed(2)}`} />
          <DetailItem label="Total Cost Basis" value={`$${totalCost.toLocaleString(undefined, { minimumFractionDigits: 2 })}`} />
          <DetailItem
            label="Total Gain/Loss"
            value={`${totalGain >= 0 ? "+" : ""}$${totalGain.toFixed(2)}`}
            className={totalGain >= 0 ? "text-profit" : "text-loss"}
          />
          <DetailItem
            label="Total Return"
            value={`${totalGainPct >= 0 ? "+" : ""}${totalGainPct.toFixed(2)}%`}
            className={totalGainPct >= 0 ? "text-profit" : "text-loss"}
          />
          <DetailItem label="Settled Qty" value={String(n(row.settled) ?? 0)} />
          <DetailItem label="Unsettled" value={String((n(row.quantity) ?? 0) - (n(row.settled) ?? 0))} />
          <DetailItem label="Day P/L %" value={mktValue > 0 ? `${(((n(row.day_pl) ?? 0) / mktValue) * 100).toFixed(2)}%` : "—"} />
          <DetailItem label="Weight" value={`${((mktValue / (n(view.liquidation_value) ?? 1)) * 100).toFixed(1)}%`} />
        </div>
      </div>
    );
  };

  return (
    <PanelCard
      title="Positions"
      subtitle="Live"
      icon={<BarChart3 className="h-3.5 w-3.5" />}
    >
      {!view.available ? (
        <p className="text-sm text-muted-foreground">{view.message ?? "Unavailable."}</p>
      ) : (
        <>
          {/* Portfolio Donut */}
          <div className="mb-4">
            <PortfolioDonut positions={view.positions} />
          </div>

          {/* Sortable & Expandable Table */}
          <SortableTable
            columns={columns}
            data={view.positions}
            emptyMessage="No open positions."
            searchable
            searchPlaceholder="Filter by symbol…"
            searchFilter={(row, query) => row.symbol.toLowerCase().includes(query)}
            onRowClick={handleRowClick}
            expandedIndex={expandedIndex}
            renderExpanded={renderExpanded}
          />

          <div className="mt-3 flex flex-wrap gap-4 border-t border-border/40 pt-3 text-xs text-muted-foreground">
            <span>
              Liquidation{" "}
              <span className="font-medium text-foreground">{money(view.liquidation_value)}</span>
            </span>
            <span>
              Cash{" "}
              <span className="font-medium text-foreground">{money(view.cash_available_for_trading)}</span>
            </span>
            <span>
              Withdrawable{" "}
              <span className="font-medium text-foreground">{money(view.cash_available_for_withdrawal)}</span>
            </span>
          </div>
        </>
      )}
    </PanelCard>
  );
}

function DetailItem({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</p>
      <p className={`mt-0.5 text-sm font-semibold ${className ?? "text-foreground"}`}>{value}</p>
    </div>
  );
}