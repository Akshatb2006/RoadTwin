"use client";

import type { ReactNode } from "react";

export function Panel({
  title,
  subtitle,
  children,
  action,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-white/10 bg-white/[0.02]">
      <header className="flex items-start justify-between gap-2 border-b border-white/10 px-3.5 py-2.5">
        <div>
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-white/70">
            {title}
          </h2>
          {subtitle && <p className="mt-0.5 text-[11px] text-white/40">{subtitle}</p>}
        </div>
        {action}
      </header>
      <div className="px-3.5 py-3">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  unit,
  delta,
  invert,
}: {
  label: string;
  value: string | number;
  unit?: string;
  /** Percent change vs baseline. */
  delta?: number;
  /** When true, an increase is bad (e.g. delay). */
  invert?: boolean;
}) {
  const good = delta === undefined ? null : invert ? delta < 0 : delta > 0;
  return (
    <div className="rounded-lg bg-white/[0.03] px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-white/40">{label}</div>
      <div className="mt-0.5 flex items-baseline gap-1">
        <span className="text-lg font-semibold tabular-nums text-white">{value}</span>
        {unit && <span className="text-[11px] text-white/45">{unit}</span>}
      </div>
      {delta !== undefined && Number.isFinite(delta) && (
        <div
          className={`mt-0.5 text-[11px] tabular-nums ${
            good === null ? "text-white/40" : good ? "text-emerald-400" : "text-rose-400"
          }`}
        >
          {delta > 0 ? "+" : ""}
          {delta.toFixed(1)}% vs baseline
        </div>
      )}
    </div>
  );
}

export function Slider({
  label,
  value,
  min,
  max,
  step,
  onChange,
  format,
  hint,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  format?: (value: number) => string;
  hint?: string;
}) {
  return (
    <label className="block">
      <div className="flex items-center justify-between text-[11px]">
        <span className="text-white/60">{label}</span>
        <span className="tabular-nums font-medium text-white/90">
          {format ? format(value) : value}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="mt-1.5 w-full accent-sky-400"
      />
      {hint && <p className="mt-0.5 text-[10px] leading-snug text-white/30">{hint}</p>}
    </label>
  );
}

export function Select<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
}) {
  return (
    <label className="block">
      <div className="text-[11px] text-white/60">{label}</div>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value as T)}
        className="mt-1 w-full rounded-md border border-white/10 bg-[#0d1119] px-2 py-1.5 text-xs text-white outline-none focus:border-sky-400/60"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Button({
  children,
  onClick,
  disabled,
  variant = "primary",
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  variant?: "primary" | "ghost" | "danger";
  className?: string;
}) {
  const styles = {
    primary:
      "bg-sky-500 text-white hover:bg-sky-400 disabled:bg-sky-500/30 disabled:text-white/40",
    ghost:
      "border border-white/15 text-white/80 hover:bg-white/5 disabled:text-white/25 disabled:hover:bg-transparent",
    danger: "bg-rose-500/90 text-white hover:bg-rose-500",
  }[variant];
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`rounded-md px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed ${styles} ${className}`}
    >
      {children}
    </button>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-xs text-white/60">
      <span className="h-3 w-3 animate-spin rounded-full border-[1.5px] border-white/25 border-t-sky-400" />
      {label}
    </span>
  );
}
