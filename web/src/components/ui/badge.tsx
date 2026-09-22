import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva("inline-flex w-fit shrink-0 items-center gap-1 whitespace-nowrap rounded-[4px] border px-1.5 py-px font-mono text-[10.5px] tracking-[0.04em] uppercase [&>svg]:size-3 [&>svg]:pointer-events-none", {
  variants: {
    variant: {
      default: "border-line bg-field text-ink-muted",
      outline: "border-line-strong bg-transparent text-ink-body",
      ink: "border-ink bg-ink text-white",
      ok: "border-ok/25 bg-ok/8 text-ok",
      warn: "border-warn/30 bg-tan text-warn",
      bad: "border-bad/25 bg-bad/6 text-bad",
      blue: "border-blue/25 bg-blue/8 text-link",
      pending: "border-dashed border-line-strong bg-transparent text-ink-tertiary",
    },
  },
  defaultVariants: { variant: "default" },
});

function Badge({ className, variant, ...props }: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
