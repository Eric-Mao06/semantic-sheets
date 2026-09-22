import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Money for non-technical readers: cents for small amounts, "< 1¢" below that. */
export function formatUsd(v: number): string {
  if (v <= 0) return "$0.00";
  if (v < 0.01) return "< 1¢";
  return USD.format(v);
}

export function formatDuration(seconds: number): string {
  if (seconds < 5) return "a few seconds";
  if (seconds < 60) return `about ${Math.round(seconds / 5) * 5} seconds`;
  const m = Math.round(seconds / 60);
  if (m < 60) return `about ${m} minute${m === 1 ? "" : "s"}`;
  const h = Math.round(m / 60);
  return `about ${h} hour${h === 1 ? "" : "s"}`;
}

export function formatCount(n: number): string {
  return n.toLocaleString("en-US");
}
