import * as React from "react";
import { Slot } from "radix-ui";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-md text-[13px] font-medium transition-[background-color,border-color,color,box-shadow] duration-150 outline-none select-none focus-visible:ring-[3px] focus-visible:ring-ring/35 disabled:pointer-events-none disabled:opacity-45 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  {
    variants: {
      variant: {
        default: "grain-dark bg-primary text-primary-foreground shadow-[inset_0_1px_0_rgba(255,255,255,0.08)] hover:bg-ink/90",
        outline: "grain border border-line-strong bg-paper text-ink hover:bg-field",
        secondary: "grain bg-field text-ink hover:bg-line",
        ghost: "text-ink-body hover:bg-field hover:text-ink",
        link: "text-link underline-offset-3 hover:underline",
        destructive: "border border-bad/30 bg-paper text-bad hover:bg-bad/5",
      },
      size: {
        default: "h-8 px-3",
        sm: "h-7 gap-1 px-2.5 text-[12.5px] [&_svg:not([class*='size-'])]:size-3",
        lg: "h-10 px-4 text-sm",
        icon: "size-8",
        "icon-sm": "size-7 [&_svg:not([class*='size-'])]:size-3.5",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

type ButtonProps = React.ComponentProps<"button"> & VariantProps<typeof buttonVariants> & { asChild?: boolean };

function Button({ className, variant, size, asChild = false, ...props }: ButtonProps) {
  const Comp = asChild ? Slot.Root : "button";
  return <Comp data-slot="button" className={cn(buttonVariants({ variant, size, className }))} {...props} />;
}

export { Button, buttonVariants };
