import { useEffect, useState } from "react";
import { api } from "../api";
import type { ColumnInfo, Question, Row } from "../types";

type Props = {
  rv: string;
  step: string;
  row: Row | null;
  column: ColumnInfo | null;
  semanticStep: string | null;
  questions: Question[];
  onCorrect: (rowId: number, column: string, value: unknown, reason: string) => Promise<void>;
};

type Prov = { raw: Record<string, unknown>; overrides: { column: string; value: unknown; reason?: string }[]; model: string };

export default function Inspector({ rv, step, row, column, semanticStep, questions, onCorrect }: Props) {
  const [full, setFull] = useState<unknown>(undefined);
  const [prov, setProv] = useState<Prov | null>(null);
  const [correction, setCorrection] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setFull(undefined);
    setProv(null);
    setCorrection("");
    if (!row || !column) return;
    const truncated = row._truncated?.includes(column.name);
    if (truncated) api.cell(rv, step, row._row_id, column.name).then((r) => setFull(r.value)).catch(() => {});
    if (semanticStep && column.name.includes(".")) api.provenance(rv, semanticStep, row._row_id).then((p) => setProv(p as Prov)).catch(() => {});
  }, [rv, step, row, column, semanticStep]);

  if (!row || !column) return <div className="body muted">Click a cell to inspect its full content, the model’s raw answer and provenance, or to correct a semantic value.</div>;

  const qName = column.name.includes(".") ? column.name.split(".")[0] : null;
  const question = questions.find((q) => q.name === qName) ?? null;
  const raw = qName && prov ? (prov.raw[qName] as Record<string, unknown> | undefined) : undefined;
  const interpreted = (prov?.raw?._interpreted as Record<string, unknown> | undefined) ?? {};
  const value = full !== undefined ? full : row[column.name];
  const canCorrect = !!question && column.name.endsWith(".value");

  return (
    <div className="body inspector">
      <div className="row">
        <span className="badge">row {row._row_id}</span>
        <span className="badge accent">{column.name}</span>
        <span className="badge">{column.type}</span>
      </div>
      <div className="field">
        <label>value</label>
        <pre>{value === null || value === undefined ? "∅ (null)" : typeof value === "object" ? JSON.stringify(value, null, 1) : String(value)}</pre>
      </div>
      {question && (
        <div className="field">
          <label>question · {question.kind}</label>
          <div style={{ fontSize: 12.5 }}>{question.instruction}</div>
        </div>
      )}
      {qName && prov && (
        <div className="field">
          <label>model answer · {prov.model}</label>
          {raw ? (
            <>
              {"noul" in raw && <Bar label="p(yes)" p={raw.noul as number} />}
              {"probabilities" in raw &&
                Object.entries(raw.probabilities as Record<string, number>)
                  .sort((a, b) => b[1] - a[1])
                  .map(([k, p]) => <Bar key={k} label={question?.kind === "score" && question.levels ? `${k} · ${question.levels[Number(k)] ?? ""}` : k} p={p} />)}
              {"confidence" in raw && <div className="muted" style={{ fontSize: 12 }}>confidence {(raw.confidence as number).toFixed(2)}{"score" in raw ? ` · expected level ${(raw.score as number).toFixed(2)}` : ""}</div>}
              <div className="muted" style={{ fontSize: 12 }}>status: {String(interpreted[`${qName}.status`] ?? "")}</div>
            </>
          ) : (
            <div className="muted">no stored answer for this row (pending, missing input or failed)</div>
          )}
          {prov.overrides.length > 0 && (
            <div className="warnings">
              {prov.overrides.map((o, i) => (
                <div key={i}>override · {o.column} → <b>{String(o.value)}</b>{o.reason ? ` (${o.reason})` : ""}</div>
              ))}
            </div>
          )}
        </div>
      )}
      {canCorrect && (
        <div className="field">
          <label>correct this value (creates a new result version; the model output is kept)</label>
          {question.kind === "category" ? (
            <select value={correction} onChange={(e) => setCorrection(e.target.value)}>
              <option value="">choose label…</option>
              {Object.keys(question.options ?? {}).map((o) => (
                <option key={o} value={o}>{o}</option>
              ))}
            </select>
          ) : question.kind === "boolean" ? (
            <select value={correction} onChange={(e) => setCorrection(e.target.value)}>
              <option value="">choose…</option>
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          ) : (
            <select value={correction} onChange={(e) => setCorrection(e.target.value)}>
              <option value="">choose level…</option>
              {(question.levels ?? []).map((l) => (
                <option key={l} value={l}>{l}</option>
              ))}
            </select>
          )}
          <input type="text" placeholder="reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} style={{ border: "1px solid var(--border)", borderRadius: 6, padding: "4px 6px" }} />
          <button
            className="btn primary"
            disabled={!correction || busy}
            onClick={async () => {
              setBusy(true);
              try {
                const v = question.kind === "boolean" ? correction === "true" : correction;
                await onCorrect(row._row_id, column.name, v, reason);
                setCorrection("");
                setReason("");
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? <span className="spinner" /> : "Save correction"}
          </button>
        </div>
      )}
    </div>
  );
}

function Bar({ label, p }: { label: string; p: number }) {
  return (
    <div className="prob">
      <span title={label} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}</span>
      <div className="bar"><div style={{ width: `${Math.round(p * 100)}%` }} /></div>
      <span className="mono">{p.toFixed(2)}</span>
    </div>
  );
}
