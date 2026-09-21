import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { ColumnInfo } from "../types";

export type LocalFilterResult = { rowIds: number[]; count: number; ms: number; column: string };

type Props = {
  rv: string;
  step: string;
  columns: ColumnInfo[];
  revision: number;
  enabled: boolean;
  onResult: (r: LocalFilterResult | null) => void;
};

type Loaded = { kind: "number"; min: number; max: number; rows: number } | { kind: "label"; labels: string[]; rows: number };

const NUMERIC = new Set(["integer", "double", "boolean"]);

/**
 * Threshold / label filter and sort over complete result vectors held in a Web Worker. Filtering and sorting
 * happen off the main thread and never hit the server; the resulting row-id list drives the grid view.
 */
export default function QuickFilter({ rv, step, columns, revision, enabled, onResult }: Props) {
  const workerRef = useRef<Worker | null>(null);
  const [column, setColumn] = useState<string>("");
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [min, setMin] = useState<number | null>(null);
  const [max, setMax] = useState<number | null>(null);
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

  const candidates = useMemo(
    () => columns.filter((c) => c.name.endsWith(".value") || c.name.endsWith(".confidence") || NUMERIC.has(c.type) || c.type === "text"),
    [columns],
  );

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
          setMin(lo);
          setMax(hi);
        } else {
          setLoaded({ kind: "label", labels: col.labels ?? [], rows: r.row_ids.length });
          setLabels(new Set((col.labels ?? []).map((_, i) => i)));
        }
        setActive(false);
        if (!r.complete) setError("Some rows are still pending; the filter reflects the current partial result.");
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
    <div className="quickfilter">
      <span className="muted">Quick filter</span>
      <select value={column} onChange={(e) => setColumn(e.target.value)}>
        <option value="">choose column…</option>
        {candidates.map((c) => (
          <option key={c.name} value={c.name}>
            {c.name} ({c.type})
          </option>
        ))}
      </select>
      {loading && <span className="spinner" />}
      {loaded?.kind === "number" && (
        <>
          <label className="row">
            min
            <input type="range" min={loaded.min} max={loaded.max} step={step_} value={min ?? loaded.min} onChange={(e) => setMin(Number(e.target.value))} />
            <input type="text" style={{ width: 64 }} value={min ?? ""} onChange={(e) => setMin(e.target.value === "" ? null : Number(e.target.value))} />
          </label>
          <label className="row">
            max
            <input type="range" min={loaded.min} max={loaded.max} step={step_} value={max ?? loaded.max} onChange={(e) => setMax(Number(e.target.value))} />
            <input type="text" style={{ width: 64 }} value={max ?? ""} onChange={(e) => setMax(e.target.value === "" ? null : Number(e.target.value))} />
          </label>
        </>
      )}
      {loaded?.kind === "label" && (
        <div className="chips">
          {loaded.labels.slice(0, 40).map((l, i) => (
            <span
              key={l}
              className="chip"
              style={{ cursor: "pointer", opacity: labels.has(i) ? 1 : 0.45, border: labels.has(i) ? "1px solid var(--accent)" : "1px solid transparent" }}
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
            </span>
          ))}
          {loaded.labels.length > 40 && <span className="muted">+{loaded.labels.length - 40} more</span>}
        </div>
      )}
      {loaded && (
        <>
          <label className="row muted">
            <input type="checkbox" checked={includeNull} onChange={(e) => setIncludeNull(e.target.checked)} /> include empty
          </label>
          <select value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
            <option value="none">no sort</option>
            <option value="desc">sort desc</option>
            <option value="asc">sort asc</option>
          </select>
          <button className="btn small primary" onClick={apply}>Apply locally</button>
          {active && <button className="btn small" onClick={clear}>Clear</button>}
          <span className="muted">{loaded.rows.toLocaleString()} rows in worker</span>
        </>
      )}
      {error && <span className="muted" style={{ color: "var(--warn)" }}>{error}</span>}
    </div>
  );
}
