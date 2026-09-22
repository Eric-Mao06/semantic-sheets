import * as React from "react";
import { cn } from "@/lib/utils";

const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(function Input({ className, type, ...props }, ref) {
  return (
    <input
      ref={ref}
      type={type}
      data-slot="input"
      className={cn(
        "flex h-8 w-full min-w-0 rounded-md border border-line-strong bg-paper px-2.5 py-1 text-[13px] text-ink shadow-none transition-[border-color,box-shadow] outline-none placeholder:text-ink-tertiary focus-visible:border-blue focus-visible:ring-[3px] focus-visible:ring-ring/25 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-bad",
        className,
      )}
      {...props}
    />
  );
});

const Textarea = React.forwardRef<HTMLTextAreaElement, React.ComponentProps<"textarea">>(function Textarea({ className, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      data-slot="textarea"
      className={cn(
        "flex min-h-14 w-full rounded-md border border-line-strong bg-paper px-2.5 py-1.5 text-[13px] leading-relaxed text-ink shadow-none outline-none transition-[border-color,box-shadow] placeholder:text-ink-tertiary focus-visible:border-blue focus-visible:ring-[3px] focus-visible:ring-ring/25 disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
});

/** Styled native select: reliable on phones and cheap in long editor forms. */
function NativeSelect({ className, children, ...props }: React.ComponentProps<"select">) {
  return (
    <span className={cn("relative inline-flex min-w-0", className)}>
      <select
        data-slot="native-select"
        className="h-8 w-full min-w-0 cursor-pointer appearance-none rounded-md border border-line-strong bg-paper pr-7 pl-2.5 text-[13px] text-ink outline-none transition-[border-color,box-shadow] focus-visible:border-blue focus-visible:ring-[3px] focus-visible:ring-ring/25 disabled:cursor-not-allowed disabled:opacity-50"
        {...props}
      >
        {children}
      </select>
      <svg aria-hidden className="pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2 text-ink-secondary" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="m6 9 6 6 6-6" />
      </svg>
    </span>
  );
}

export { Input, Textarea, NativeSelect };
