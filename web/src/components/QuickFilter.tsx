import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api";
import type { ColumnInfo } from "@/types";
import { cn, formatCount } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { Spinner } from "@/components/ui/misc";
import { Slider } from "@/components/ui/slider";

export type LocalFilterResult = { rowIds: number[]; count: number; ms: number; column: string };

type Props = {
  rv: string;
  step: string;
  columns: ColumnInfo[];
  revision: number;
  enabled: boolean;
  /** The parent owns the toggle (it sits in the views row); the panel stays mounted so the worker keeps its vectors. */
  open: boolean;
  onResult: (r: LocalFilterResult | null) => void;
};

type Loaded = { kind: "number"; min: number; max: number; rows: number } | { kind: "label"; labels: string[]; rows: number };

const NUMERIC = new Set(["integer", "double", "boolean"]);

/**
 * Threshold / label filter and sort over complete result vectors held in a Web Worker. Filtering and sorting
 * happen off the main thread and never hit the server; the resulting row-id list drives the grid view.
 * Collapsed behind a single “Filter” button in the views row so the table stays uncluttered.
 */
export default function QuickFilter({ rv, step, columns, revision, enabled, open, onResult }: Props) {
  const workerRef = useRef<Worker | null>(null);
  const [column, setColumn] = useState<string>("");
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Kept as text so partial input like "0." survives re-renders; parsed when applied.
  const [minText, setMinText] = useState("");
  const [maxText, setMaxText] = useState("");
  const parseBound = (t: string): number | null => (t.trim() === "" || Number.isNaN(Number(t)) ? null : Number(t));
  const min = parseBound(minText);
  const max = parseBound(maxText);
  const [labels, setLabels] = useState<Set<number>>(new Set());
  const [includeNull, setIncludeNull] = useState(false);
  const [sort, setSort] = useState<"none" | "asc" | "desc">("none");
  const [active, setActive] = useState(false);
  const reqId = useRef(0);
  const pending = useRef(new Map<number, (m: { count: number; rowIds: ArrayBuffer; ms: number }) => void>());

  // Created in an effect (not useMemo) so StrictMode's mount/unmount/mount cycle cannot leave a terminated worker behind.
  useEffect(() => {
    const worker = new Worker(new URL("../worker/vectors.worker.ts", import.meta.url), { type: "module" });
    worker.onmessage = (e: MessageEvent) => {
      const m = e.data;
      if (m.type === "result") pending.current.get(m.id)?.(m);
      if (m.type === "over_budget") setError(`Vector for ${m.column} exceeds the local memory budget`);
    };
    workerRef.current = worker;
    return () => {
      worker.terminate();
      workerRef.current = null;
    };
  }, []);

  const candidates = useMemo(() => columns.filter((c) => c.name.endsWith(".value") || c.name.endsWith(".confidence") || NUMERIC.has(c.type) || c.type === "text"), [columns]);

  // Reset whenever the view changes.
  useEffect(() => {
    setColumn("");
    setLoaded(null);
    setActive(false);
    setError(null);
    workerRef.current?.postMessage({ type: "clear" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rv, step]);

  useEffect(() => {
    if (!column) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .vectors(rv, step, [column])
      .then((r) => {
        if (cancelled) return;
        const col = r.columns[column];
        workerRef.current?.postMessage({ type: "load", rowIds: r.row_ids, columns: r.columns, replace: true });
        if (col.kind === "number") {
          const nums = col.values.filter((v): v is number => v !== null);
          const lo = nums.length ? Math.min(...nums) : 0;
          const hi = nums.length ? Math.max(...nums) : 1;
          setLoaded({ kind: "number", min: lo, max: hi, rows: r.row_ids.length });
          setMinText(String(lo));
          setMaxText(String(hi));
        } else {
          setLoaded({ kind: "label", labels: col.labels ?? [], rows: r.row_ids.length });
          setLabels(new Set((col.labels ?? []).map((_, i) => i)));
        }
        setActive(false);
        if (!r.complete) setError("Some rows are still being worked on; the filter reflects the current partial result.");
      })
      .catch((e) => !cancelled && setError((e as Error).message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [column, rv, step, revision]);

  const apply = () => {
    if (!loaded) return;
    const id = ++reqId.current;
    const conditions = loaded.kind === "number" ? [{ column, min, max, includeNull }] : [{ column, labels: [...labels], includeNull }];
    pending.current.set(id, (m) => {
      pending.current.delete(id);
      if (id !== reqId.current) return;
      const ids = Array.from(new Int32Array(m.rowIds));
      setActive(true);
      onResult({ rowIds: ids, count: m.count, ms: m.ms, column });
    });
    workerRef.current?.postMessage({ type: "filter", id, conditions, sort: sort === "none" ? null : { column, direction: sort } });
  };

  const clear = () => {
    setActive(false);
    onResult(null);
  };

  if (!enabled || candidates.length === 0) return null;

  const step_ = loaded?.kind === "number" ? (loaded.max - loaded.min) / 100 || 0.01 : 1;

  return (
    <div className="border-b border-line bg-paper" hidden={!open}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2">
        <NativeSelect className="min-w-40" value={column} onChange={(e) => setColumn(e.target.value)}>
          <option value="">Choose a column…</option>
          {candidates.map((c) => (
            <option key={c.name} value={c.name}>{c.name}</option>
          ))}
        </NativeSelect>
        {loading && <Spinner />}
        {loaded?.kind === "number" && (
          <div className="flex min-w-64 flex-1 items-center gap-2">
            <Input type="text" inputMode="decimal" className="w-18 font-mono text-[12px]" value={minText} onChange={(e) => setMinText(e.target.value)} aria-label="Minimum" />
            <Slider className="flex-1" min={loaded.min} max={loaded.max} step={step_} value={[min ?? loaded.min, max ?? loaded.max]} onValueChange={([a, b]) => { setMinText(String(a)); setMaxText(String(b)); }} />
            <Input type="text" inputMode="decimal" className="w-18 font-mono text-[12px]" value={maxText} onChange={(e) => setMaxText(e.target.value)} aria-label="Maximum" />
          </div>
        )}
        {loaded?.kind === "label" && (
          <div className="flex flex-wrap gap-1">
            {loaded.labels.slice(0, 40).map((l, i) => (
              <button
                type="button"
                key={l}
                className={cn("rounded-[4px] border px-1.5 py-px font-mono text-[11.5px] transition-colors", labels.has(i) ? "border-ink bg-ink text-white" : "border-line bg-paper text-ink-muted hover:border-line-strong")}
                onClick={() =>
                  setLabels((s) => {
                    const n = new Set(s);
                    if (n.has(i)) n.delete(i);
                    else n.add(i);
                    return n;
                  })
                }
              >
                {l}
              </button>
            ))}
            {loaded.labels.length > 40 && <span className="text-[12px] text-ink-muted">+{loaded.labels.length - 40} more</span>}
          </div>
        )}
        {loaded && (
          <>
            <label className="flex items-center gap-1.5 text-[12px] text-ink-muted">
              <input type="checkbox" className="accent-ink" checked={includeNull} onChange={(e) => setIncludeNull(e.target.checked)} /> include empty
            </label>
            <NativeSelect className="w-auto" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
              <option value="none">keep order</option>
              <option value="desc">highest first</option>
              <option value="asc">lowest first</option>
            </NativeSelect>
            <Button size="sm" onClick={apply}>Apply</Button>
            {active && <Button variant="ghost" size="sm" onClick={clear}>Clear</Button>}
            <span className="text-[11.5px] text-ink-tertiary">{formatCount(loaded.rows)} rows</span>
          </>
        )}
        {error && <span className="text-[12px] text-warn">{error}</span>}
      </div>
    </div>
  );
}
