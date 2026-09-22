import { useEffect, useState, type ReactNode } from "react";
import { api } from "@/api";
import type { ColumnInfo, Question, Row } from "@/types";
import { humanColumn } from "@/lib/describe";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input, NativeSelect } from "@/components/ui/input";
import { Notice, Spinner } from "@/components/ui/misc";

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

/** One cell: the value, what the model was asked, how sure it was. Changing the answer is tucked away. */
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

  if (!row || !column) {
    return (
      <div className="flex flex-col gap-2 p-4">
        <p className="font-display text-[20px] leading-tight text-ink">Pick a cell.</p>
        <p className="text-[12.5px] leading-relaxed text-ink-muted">Click any cell in the table to read its full content and, for answers the model produced, see why it answered that way.</p>
      </div>
    );
  }

  const qName = column.name.includes(".") ? column.name.split(".")[0] : null;
  const question = questions.find((q) => q.name === qName) ?? null;
  const raw = qName && prov ? (prov.raw[qName] as Record<string, unknown> | undefined) : undefined;
  const interpreted = (prov?.raw?._interpreted as Record<string, unknown> | undefined) ?? {};
  const value = full !== undefined ? full : row[column.name];
  const canCorrect = !!question && column.name.endsWith(".value");
  const status = String(interpreted[`${qName}.status`] ?? "");

  return (
    <div className="flex flex-col gap-4 overflow-y-auto p-4">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="outline">row {row._row_id}</Badge>
        <Badge variant="ink" className="normal-case tracking-normal">{column.name}</Badge>
        <Badge>{column.type}</Badge>
      </div>

      <Field label={question ? "Answer" : "Value"}>
        <pre className="m-0 max-h-64 overflow-auto rounded-md border border-line bg-field/70 p-2.5 font-sans text-[13px] leading-relaxed break-words whitespace-pre-wrap text-ink">
          {value === null || value === undefined ? <span className="text-ink-tertiary">empty</span> : typeof value === "object" ? JSON.stringify(value, null, 1) : typeof value === "boolean" ? (value ? "yes" : "no") : String(value)}
        </pre>
      </Field>

      {question && (
        <Field label="What the model was asked">
          <p className="text-[12.5px] leading-relaxed text-ink-body">{question.instruction}</p>
          {question.kind === "category" && question.options && (
            <p className="mt-1 text-[12px] text-ink-muted">Choices: {Object.keys(question.options).join(", ")}</p>
          )}
        </Field>
      )}

      {qName && !prov && semanticStep && (
        <div className="flex items-center gap-2 text-[12px] text-ink-muted"><Spinner /> Loading the model's reasoning…</div>
      )}

      {qName && prov && (
        <Field label="How sure it was">
          {raw ? (
            <div className="flex flex-col gap-1.5">
              {"noul" in raw && <Bar label="yes" p={raw.noul as number} />}
              {"probabilities" in raw &&
                Object.entries(raw.probabilities as Record<string, number>)
                  .sort((a, b) => b[1] - a[1])
                  .slice(0, 6)
                  .map(([k, p]) => <Bar key={k} label={question?.kind === "score" && question.levels ? question.levels[Number(k)] ?? k : k} p={p} />)}
              <div className="flex flex-wrap gap-x-3 pt-0.5 text-[11.5px] text-ink-muted">
                {"confidence" in raw && <span>confidence {(raw.confidence as number).toFixed(2)}</span>}
                {status && <span>status {status}</span>}
                <span className="font-mono">{prov.model}</span>
              </div>
            </div>
          ) : (
            <p className="text-[12.5px] text-ink-muted">No stored answer for this row yet (still pending, missing input or failed).</p>
          )}
          {prov.overrides.length > 0 && (
            <Notice tone="warn" className="mt-2">
              {prov.overrides.map((o, i) => (
                <div key={i}>
                  Changed by hand: {humanColumn(o.column)} → <b>{String(o.value)}</b>
                  {o.reason ? ` (${o.reason})` : ""}
                </div>
              ))}
            </Notice>
          )}
        </Field>
      )}

      {canCorrect && (
        <Collapsible className="flex flex-col gap-3 border-t border-line pt-3">
          <CollapsibleTrigger className="self-start">Change this answer</CollapsibleTrigger>
          <CollapsibleContent className="flex flex-col gap-2">
            <p className="text-[12px] leading-relaxed text-ink-muted">Your change is saved as a new version of the result. The model's original answer is kept.</p>
            <NativeSelect value={correction} onChange={(e) => setCorrection(e.target.value)}>
              <option value="">Choose…</option>
              {question.kind === "category"
                ? Object.keys(question.options ?? {}).map((o) => (
                    <option key={o} value={o}>{o}</option>
                  ))
                : question.kind === "boolean"
                  ? [<option key="t" value="true">yes</option>, <option key="f" value="false">no</option>]
                  : (question.levels ?? []).map((l) => (
                      <option key={l} value={l}>{l}</option>
                    ))}
            </NativeSelect>
            <Input type="text" placeholder="Why? (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
            <Button
              variant="outline"
              className="self-start"
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
              {busy ? <Spinner /> : null}
              Save change
            </Button>
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="label-mono">{label}</span>
      {children}
    </div>
  );
}

function Bar({ label, p }: { label: string; p: number }) {
  return (
    <div className="grid grid-cols-[minmax(80px,1fr)_2fr_40px] items-center gap-2 text-[12px]">
      <span title={label} className="truncate text-ink-body">{label}</span>
      <div className="h-1.5 overflow-hidden rounded-full bg-line">
        <div className="h-full bg-ink" style={{ width: `${Math.round(p * 100)}%` }} />
      </div>
      <span className="text-right font-mono text-[11px] text-ink-muted tabular-nums">{p.toFixed(2)}</span>
    </div>
  );
}
