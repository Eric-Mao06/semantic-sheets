/// <reference lib="webworker" />
/**
 * Holds complete score/label vectors for the current result and answers threshold/label filters and sorts
 * without touching the main thread or the server. Uses the same null / tie-break rules as the server:
 * nulls never match a numeric condition, sorts put nulls last and break ties by ascending row id.
 */

type NumberVec = { kind: "number"; values: Float64Array };
type LabelVec = { kind: "label"; values: Int32Array; labels: string[] };
type Vec = NumberVec | LabelVec;

type Condition = { column: string; min?: number | null; max?: number | null; labels?: number[] | null; includeNull?: boolean };
type SortSpec = { column: string; direction: "asc" | "desc" } | null;

let rowIds = new Int32Array(0);
const vectors = new Map<string, Vec>();
const sortedCache = new Map<string, Int32Array>();
let budgetBytes = 64 * 1024 * 1024;
let usedBytes = 0;

type InMsg =
  | { type: "load"; rowIds: number[]; columns: Record<string, { kind: "number" | "label"; values: (number | null)[]; labels?: string[] }>; replace?: boolean }
  | { type: "filter"; id: number; conditions: Condition[]; sort: SortSpec }
  | { type: "clear" }
  | { type: "budget"; bytes: number };

self.onmessage = (e: MessageEvent<InMsg>) => {
  const msg = e.data;
  if (msg.type === "budget") {
    budgetBytes = msg.bytes;
    return;
  }
  if (msg.type === "clear") {
    vectors.clear();
    sortedCache.clear();
    usedBytes = 0;
    rowIds = new Int32Array(0);
    return;
  }
  if (msg.type === "load") {
    if (msg.replace) {
      vectors.clear();
      sortedCache.clear();
      usedBytes = 0;
    }
    rowIds = Int32Array.from(msg.rowIds);
    for (const [name, col] of Object.entries(msg.columns)) {
      const n = col.values.length;
      const bytes = n * (col.kind === "number" ? 8 : 4);
      if (usedBytes + bytes > budgetBytes) {
        (self as unknown as Worker).postMessage({ type: "over_budget", column: name, usedBytes, budgetBytes });
        continue;
      }
      if (col.kind === "number") {
        const arr = new Float64Array(n);
        for (let i = 0; i < n; i++) arr[i] = col.values[i] === null ? NaN : (col.values[i] as number);
        vectors.set(name, { kind: "number", values: arr });
      } else {
        const arr = new Int32Array(n);
        for (let i = 0; i < n; i++) arr[i] = col.values[i] === null ? -1 : (col.values[i] as number);
        vectors.set(name, { kind: "label", values: arr, labels: col.labels ?? [] });
      }
      usedBytes += bytes;
      sortedCache.delete(name);
    }
    (self as unknown as Worker).postMessage({ type: "loaded", rows: rowIds.length, columns: [...vectors.keys()], usedBytes });
    return;
  }
  if (msg.type === "filter") {
    const t0 = performance.now();
    const n = rowIds.length;
    const mask = new Uint8Array(n).fill(1);
    for (const c of msg.conditions) {
      const v = vectors.get(c.column);
      if (!v) continue;
      if (v.kind === "number") {
        const lo = c.min ?? -Infinity;
        const hi = c.max ?? Infinity;
        for (let i = 0; i < n; i++) {
          if (!mask[i]) continue;
          const x = v.values[i];
          if (Number.isNaN(x)) {
            if (!c.includeNull) mask[i] = 0;
          } else if (x < lo || x > hi) mask[i] = 0;
        }
      } else if (c.labels && c.labels.length) {
        const allowed = new Set(c.labels);
        for (let i = 0; i < n; i++) {
          if (!mask[i]) continue;
          const x = v.values[i];
          if (x === -1 ? !c.includeNull : !allowed.has(x)) mask[i] = 0;
        }
      }
    }
    let order: Int32Array;
    if (msg.sort && vectors.has(msg.sort.column)) {
      order = sortedIndex(msg.sort.column);
      if (msg.sort.direction === "desc") {
        // reverse but keep nulls last and ties by ascending row id
        const v = vectors.get(msg.sort.column)!;
        const nonNull: number[] = [];
        const nulls: number[] = [];
        for (let k = 0; k < order.length; k++) {
          const i = order[k];
          const isNull = v.kind === "number" ? Number.isNaN(v.values[i]) : v.values[i] === -1;
          (isNull ? nulls : nonNull).push(i);
        }
        const desc = stableDesc(nonNull, v);
        order = Int32Array.from(desc.concat(nulls));
      }
    } else {
      order = new Int32Array(n);
      for (let i = 0; i < n; i++) order[i] = i;
    }
    let count = 0;
    for (let i = 0; i < n; i++) if (mask[i]) count++;
    const out = new Int32Array(count);
    let k = 0;
    for (let j = 0; j < order.length; j++) {
      const i = order[j];
      if (mask[i]) out[k++] = rowIds[i];
    }
    const buf = out.buffer;
    (self as unknown as Worker).postMessage({ type: "result", id: msg.id, count, rowIds: buf, ms: performance.now() - t0 }, [buf]);
  }
};

function sortedIndex(column: string): Int32Array {
  const cached = sortedCache.get(column);
  if (cached) return cached;
  const v = vectors.get(column)!;
  const n = rowIds.length;
  const idx = Array.from({ length: n }, (_, i) => i);
  if (v.kind === "number") {
    idx.sort((a, b) => {
      const x = v.values[a];
      const y = v.values[b];
      const xn = Number.isNaN(x);
      const yn = Number.isNaN(y);
      if (xn && yn) return rowIds[a] - rowIds[b];
      if (xn) return 1;
      if (yn) return -1;
      return x - y || rowIds[a] - rowIds[b];
    });
  } else {
    idx.sort((a, b) => {
      const x = v.values[a];
      const y = v.values[b];
      if (x === -1 && y === -1) return rowIds[a] - rowIds[b];
      if (x === -1) return 1;
      if (y === -1) return -1;
      const c = v.labels[x].localeCompare(v.labels[y]);
      return c || rowIds[a] - rowIds[b];
    });
  }
  const arr = Int32Array.from(idx);
  sortedCache.set(column, arr);
  return arr;
}

function stableDesc(nonNull: number[], v: Vec): number[] {
  return nonNull.sort((a, b) => {
    if (v.kind === "number") return v.values[b] - v.values[a] || rowIds[a] - rowIds[b];
    return v.labels[v.values[b]].localeCompare(v.labels[v.values[a]]) || rowIds[a] - rowIds[b];
  });
}
