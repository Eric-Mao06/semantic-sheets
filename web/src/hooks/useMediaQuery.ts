import { useEffect, useState } from "react";

/** Widths at or below this render the single-pane mobile layout; keep in sync with the media queries in styles.css. */
export const MOBILE_QUERY = "(max-width: 900px)";

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() => (typeof window !== "undefined" && "matchMedia" in window ? window.matchMedia(query).matches : false));
  useEffect(() => {
    if (typeof window === "undefined" || !("matchMedia" in window)) return;
    const mq = window.matchMedia(query);
    const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}
