import * as React from "react";
import { Separator as SeparatorPrimitive, Label as LabelPrimitive } from "radix-ui";
import { LoaderCircle } from "lucide-react";
import { cn } from "@/lib/utils";

function Separator({ className, orientation = "horizontal", decorative = true, ...props }: React.ComponentProps<typeof SeparatorPrimitive.Root>) {
  return (
    <SeparatorPrimitive.Root
      data-slot="separator"
      decorative={decorative}
      orientation={orientation}
      className={cn("shrink-0 bg-line data-[orientation=horizontal]:h-px data-[orientation=horizontal]:w-full data-[orientation=vertical]:h-full data-[orientation=vertical]:w-px", className)}
      {...props}
    />
  );
}

function Label({ className, ...props }: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return <LabelPrimitive.Root data-slot="label" className={cn("label-mono flex items-center gap-1.5 select-none", className)} {...props} />;
}

function Spinner({ className, ...props }: React.ComponentProps<typeof LoaderCircle>) {
  return <LoaderCircle role="status" aria-label="Loading" className={cn("size-3.5 animate-spin text-ink-secondary", className)} {...props} />;
}

function Kbd({ className, ...props }: React.ComponentProps<"kbd">) {
  return <kbd data-slot="kbd" className={cn("inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-[3px] border border-line-strong bg-field px-1 font-mono text-[10px] text-ink-muted", className)} {...props} />;
}

/** Small stat: mono caps label over a value, as on a spec sheet. */
function Stat({ label, value, hint, className }: { label: string; value: React.ReactNode; hint?: string; className?: string }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-0.5", className)} title={hint}>
      <span className="label-mono">{label}</span>
      <span className="text-[13px] text-ink tabular-nums">{value}</span>
    </div>
  );
}

/** Inline notice. `tone` picks the paper tint. */
function Notice({ tone = "neutral", className, children, ...props }: React.ComponentProps<"div"> & { tone?: "neutral" | "warn" | "bad" | "ok" }) {
  const tones = {
    neutral: "border-line bg-field text-ink-body",
    warn: "border-tan-med bg-tan text-ink-body",
    bad: "border-bad/25 bg-pink text-bad",
    ok: "border-ok/20 bg-aqua text-ink-body",
  } as const;
  return (
    <div role={tone === "bad" ? "alert" : undefined} className={cn("grain rounded-md border px-3 py-2 text-[12.5px] leading-relaxed", tones[tone], className)} {...props}>
      {children}
    </div>
  );
}

export { Separator, Label, Spinner, Kbd, Stat, Notice };
