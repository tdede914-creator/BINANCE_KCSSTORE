export function formatPrice(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined) return "—";
  const d = v >= 1000 ? 2 : v >= 1 ? 4 : 6;
  return v.toLocaleString(undefined, {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

export function formatUsdt(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString();
}

export function classFor(side: string): string {
  return side === "LONG" ? "text-long" : "text-short";
}

export function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return "text-muted";
  if (v > 0) return "text-long";
  if (v < 0) return "text-short";
  return "text-muted";
}
