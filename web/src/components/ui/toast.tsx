import * as React from "react";
import { cn } from "@/lib/utils";

/** Single transient message, bottom-centred. The caller owns the timer. */
function Toast({ className, children, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      role="status"
      className={cn(
        "grain-dark fixed bottom-6 left-1/2 z-50 max-w-[calc(100vw-2rem)] -translate-x-1/2 rounded-md bg-ink px-3.5 py-2 text-center text-[12.5px] text-white shadow-lg animate-in fade-in-0 slide-in-from-bottom-2",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}

export { Toast };
