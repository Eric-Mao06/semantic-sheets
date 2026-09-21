import { useEffect, useState } from "react";
import { api } from "../api";

interface Detail { row_id: number; values: Record<string, unknown>; columns: Record<string, string>; overrides: { column: string; value: unknown; provenance: Record<string, unknown>; created_at: string }[]; }

export function Inspector({ rv, rowId, onCorrect, onDatasetCell }: { rv: string | null; rowId: number | null; onCorrect: (rowId: number, column: string, value: unknown) => Promise<void>; onDatasetCell?: (rowId: number) => Promise<Record<string, unknown>> }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [editing, setEditing] = useState<{ column: string; value: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setDetail(null); setError(null);
    if (rowId === null) return;
    if (rv) api.rowDetail(rv, rowId).then(setDetail).catch((e) => setError(e.message));
    else if (onDatasetCell) onDatasetCell(rowId).then((values) => setDetail({ row_id: rowId, values, columns: {}, overrides: [] })).catch((e) => setError(e.message));
  }, [rv, rowId]);
  if (rowId === null) return <div className="panel muted">Select a cell to inspect the row, its raw model output, and corrections.</div>;
  if (error) return <div className="panel error">{error}</div>;
  if (!detail) return <div className="panel muted">Loading row {rowId}…</div>;
  const entries = Object.entries(detail.values);
  const raws = entries.filter(([k]) => k.endsWith(".raw"));
  return (
    <div className="panel" data-testid="inspector">
      <div><b>Row {detail.row_id}</b> {detail.overrides.length > 0 && <span className="pill warn">{detail.overrides.length} correction(s)</span>}</div>
      <div className="kv">
        {entries.filter(([k]) => !k.endsWith(".raw")).map(([k, v]) => (
          <>
            <div className="k" key={k + "k"}>{k}</div>
            <div className="v" key={k + "v"}>
              {editing?.column === k ? (
                <span className="row">
                  <input value={editing.value} onChange={(e) => setEditing({ column: k, value: e.target.value })} autoFocus data-testid="inspector-edit-input" />
                  <button className="primary" onClick={async () => { const val = coerce(editing.value, detail.columns[k]); await onCorrect(detail.row_id, k, val); setEditing(null); }} data-testid="inspector-edit-save">Save</button>
                  <button onClick={() => setEditing(null)}>Cancel</button>
                </span>
              ) : (
                <span onDoubleClick={() => { if (k !== "_row_id" && !k.endsWith(".status")) setEditing({ column: k, value: v == null ? "" : String(v) }); }} title="double-click to correct" data-testid={`cell-${k}`}>
                  {v === null || v === undefined ? <span className="muted">null</span> : String(v)}
                  {detail.overrides.some((o) => o.column === k) && <span className="pill warn" style={{ marginLeft: 6 }}>override</span>}
                </span>
              )}
            </div>
          </>
        ))}
      </div>
      {raws.length > 0 && (
        <details open>
          <summary>Raw model output (immutable)</summary>
          {raws.map(([k, v]) => <div key={k}><div className="muted">{k}</div><pre className="mono">{typeof v === "string" ? v : JSON.stringify(v, null, 1)}</pre></div>)}
        </details>
      )}
      {detail.overrides.length > 0 && (
        <details>
          <summary>Corrections</summary>
          {detail.overrides.map((o, i) => <div key={i} className="muted">{o.column} → {String(o.value)} · {JSON.stringify(o.provenance)} · {o.created_at}</div>)}
        </details>
      )}
      <div className="muted">Double-click a value to record a correction. Corrections create a new result version; model output is never altered.</div>
    </div>
  );
}

function coerce(s: string, type: string | undefined): unknown {
  if (s === "" || s === "null") return null;
  if (type === "boolean") return ["true", "1", "yes"].includes(s.toLowerCase());
  if (type === "number" || type === "integer") { const n = Number(s); return Number.isNaN(n) ? s : n; }
  return s;
}
