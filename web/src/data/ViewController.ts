import { api } from "../api";
import type { QueryResponse, Row } from "../types";

/** What the grid is looking at: one step of one result version, a fixed column list, optionally restricted to
 *  the row ids a local (worker-side) filter produced, in that order. */
export type ViewSpec = {
  rv: string;
  step: string;
  columns: string[];
  revision: number;
  localRowIds?: number[] | null;
};

export type ViewMeta = {
  total: number | null;
  denominator: number | null;
  countStatus: "complete" | "partial" | "n/a";
  loading: boolean;
  error: string | null;
};

type Block = { rows: Row[]; bytes: number; revision: number; touched: number };

const BLOCK = 128; // rows per request; the web surface caps a page at 256 rows / 256 KB
const MAX_BLOCKS = 400; // ~51k rows resident before the least recently shown blocks are dropped
const CELL_CHARS = 512;

function sameIds(a: number[] | null | undefined, b: number[] | null | undefined): boolean {
  if (!a && !b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/**
 * Block cache between the server's paged query API and Glide's cell callbacks.
 *
 * Glide asks for cells synchronously; the controller answers from loaded blocks and marks everything else
 * missing (Glide draws a skeleton), while `ensureVisible` fetches the blocks under the viewport plus one in the
 * scroll direction. Listeners are notified when a block lands so the grid re-reads. Responses that belong to an
 * earlier spec (generation) are dropped, and job progress can invalidate the rows a committed chunk touched.
 */
export class ViewController {
  spec: ViewSpec;
  generation = 0;
  meta: ViewMeta = { total: null, denominator: null, countStatus: "n/a", loading: false, error: null };

  private blocks = new Map<number, Block>();
  private inflight = new Map<number, Promise<void>>();
  private aborts = new Set<AbortController>();
  private listeners = new Set<() => void>();
  private notifyScheduled = false;
  private touch = 0;

  constructor(spec: ViewSpec) {
    this.spec = spec;
  }

  // ---- spec / lifecycle -------------------------------------------------------------------------

  setSpec(spec: ViewSpec): void {
    const prev = this.spec;
    const sameView = prev.rv === spec.rv && prev.step === spec.step && sameIds(prev.localRowIds, spec.localRowIds) && prev.columns.join("\u0000") === spec.columns.join("\u0000");
    this.spec = spec;
    if (sameView && prev.revision === spec.revision) return;
    // A revision bump on the same view keeps the rows on screen and refreshes them underneath; anything else is a
    // different table and the cache is meaningless.
    this.cancelInflight();
    if (!sameView) {
      this.blocks.clear();
      this.meta = { total: spec.localRowIds ? spec.localRowIds.length : null, denominator: null, countStatus: spec.localRowIds ? "complete" : "n/a", loading: false, error: null };
    } else {
      for (const b of this.blocks.values()) b.revision = -1; // stale: re-fetch when visible, keep showing meanwhile
    }
    this.generation++;
    this.notify();
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  }

  // ---- reads (synchronous, for Glide) ------------------------------------------------------------

  rowCount(): number {
    if (this.spec.localRowIds) return this.spec.localRowIds.length;
    return this.meta.total ?? 0;
  }

  getRow(displayIndex: number): Row | undefined {
    const b = this.blocks.get(Math.floor(displayIndex / BLOCK));
    if (!b) return undefined;
    b.touched = ++this.touch;
    return b.rows[displayIndex % BLOCK];
  }

  getCell(displayIndex: number, column: string): { value: unknown; truncated: boolean; missing: boolean } {
    const row = this.getRow(displayIndex);
    if (!row) return { value: undefined, truncated: false, missing: true };
    return { value: row[column], truncated: !!row._truncated?.includes(column), missing: false };
  }

  cacheStats(): { blocks: number; bytes: number } {
    let bytes = 0;
    for (const b of this.blocks.values()) bytes += b.bytes;
    return { blocks: this.blocks.size, bytes };
  }

  // ---- loading -----------------------------------------------------------------------------------

  /** Make sure display rows [from, to) are loaded or loading, prefetching one block in the scroll direction. */
  ensureVisible(from: number, to: number, dir: 1 | -1 = 1): void {
    if (!this.spec.rv) return;
    const first = Math.max(0, Math.floor(from / BLOCK));
    let last = Math.max(first, Math.floor(Math.max(from, to - 1) / BLOCK));
    const known = this.rowCount();
    if (known > 0) last = Math.min(last, Math.floor((known - 1) / BLOCK));
    const wanted: number[] = [];
    for (let b = first; b <= last; b++) wanted.push(b);
    const ahead = dir === 1 ? last + 1 : first - 1;
    if (ahead >= 0 && (known === 0 || ahead * BLOCK < known)) wanted.push(ahead);
    for (const b of wanted) {
      const have = this.blocks.get(b);
      if (have && have.revision === this.spec.revision) continue;
      if (!this.inflight.has(b)) this.load(b);
    }
  }

  /**
   * A job committed rows with `_row_id` in [startId, endId] at `revision`. Drop the blocks that hold any of them so
   * the next paint re-reads them; blocks not on screen are simply forgotten.
   */
  invalidateRowRange(startId: number, endId: number, revision: number): void {
    if (this.spec.localRowIds) return; // local filters are re-run explicitly by the user
    let hit = false;
    for (const [idx, b] of this.blocks) {
      const rows = b.rows;
      if (!rows.length) continue;
      const lo = rows[0]._row_id;
      const hi = rows[rows.length - 1]._row_id;
      if (hi >= startId && lo <= endId) {
        this.blocks.delete(idx);
        hit = true;
      }
    }
    if (revision > this.spec.revision) this.spec = { ...this.spec, revision };
    if (hit) this.notify();
  }

  // ---- internals ---------------------------------------------------------------------------------

  private load(index: number): void {
    const gen = this.generation;
    const spec = this.spec;
    const ac = new AbortController();
    this.aborts.add(ac);
    const p = this.fetchBlock(index, spec, ac.signal)
      .then((block) => {
        if (gen !== this.generation || !block) return;
        this.put(index, block);
        this.meta.error = null;
      })
      .catch((e: unknown) => {
        if (gen !== this.generation || (e instanceof DOMException && e.name === "AbortError")) return;
        this.meta.error = e instanceof Error ? e.message : String(e);
      })
      .finally(() => {
        this.aborts.delete(ac);
        this.inflight.delete(index);
        this.meta.loading = this.inflight.size > 0;
        this.notify();
      });
    this.inflight.set(index, p);
    this.meta.loading = true;
  }

  private async fetchBlock(index: number, spec: ViewSpec, signal: AbortSignal): Promise<Block | null> {
    const columns = spec.columns.length ? spec.columns : undefined;
    const rows: Row[] = [];
    let bytes = 0;
    if (spec.localRowIds) {
      const ids = spec.localRowIds.slice(index * BLOCK, (index + 1) * BLOCK);
      if (!ids.length) return null;
      const r = await api.query(spec.rv, { step: spec.step, columns, row_ids: ids, max_cell_chars: CELL_CHARS }, signal);
      bytes += JSON.stringify(r.rows).length;
      // The server returns rows in the requested order; keep positions stable if any id is no longer present.
      const byId = new Map(r.rows.map((row) => [row._row_id, row] as const));
      for (const id of ids) rows.push(byId.get(id) ?? ({ _row_id: id } as Row));
    } else {
      // Pages are bounded by bytes as well as rows, so a block may take more than one request to fill.
      let start = index * BLOCK;
      const end = start + BLOCK;
      let first = true;
      while (start < end) {
        const r: QueryResponse = await api.query(spec.rv, { step: spec.step, columns, start, limit: end - start, max_cell_chars: CELL_CHARS }, signal);
        if (first) {
          first = false;
          this.meta.total = r.total_count;
          this.meta.denominator = r.denominator;
          this.meta.countStatus = r.count_status;
        }
        bytes += JSON.stringify(r.rows).length;
        rows.push(...r.rows);
        if (!r.rows.length || r.next_start === null || r.next_start >= end) break;
        start = r.next_start;
      }
    }
    return { rows, bytes, revision: spec.revision, touched: ++this.touch };
  }

  private put(index: number, block: Block): void {
    this.blocks.set(index, block);
    if (this.blocks.size > MAX_BLOCKS) {
      const victims = [...this.blocks.entries()].sort((a, b) => a[1].touched - b[1].touched).slice(0, this.blocks.size - MAX_BLOCKS);
      for (const [idx] of victims) if (!this.inflight.has(idx)) this.blocks.delete(idx);
    }
  }

  private cancelInflight(): void {
    for (const ac of this.aborts) ac.abort();
    this.aborts.clear();
    this.inflight.clear();
    this.meta.loading = false;
  }

  private notify(): void {
    if (this.notifyScheduled) return;
    this.notifyScheduled = true;
    queueMicrotask(() => {
      this.notifyScheduled = false;
      for (const fn of this.listeners) fn();
    });
  }
}
