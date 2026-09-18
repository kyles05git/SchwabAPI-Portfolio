import type { Num } from "./types";

/** Coerce a pydantic-serialized numeric (string or number) to a JS number. */
export function n(value: Num | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function money(value: Num | null | undefined): string {
  const parsed = n(value);
  return parsed === null ? "—" : usd.format(parsed);
}

export function pct(value: Num | null | undefined, signed = false): string {
  const parsed = n(value);
  if (parsed === null) return "—";
  const sign = signed && parsed > 0 ? "+" : "";
  return `${sign}${parsed.toFixed(2)}%`;
}

export function qty(value: Num | null | undefined): string {
  const parsed = n(value);
  return parsed === null ? "—" : String(parsed);
}

/** CSS class for profit/loss/neutral coloring. */
export function signClass(value: Num | null | undefined): string {
  const parsed = n(value);
  if (parsed === null) return "text-muted-foreground";
  if (parsed > 0) return "text-profit";
  if (parsed < 0) return "text-loss";
  return "text-muted-foreground";
}