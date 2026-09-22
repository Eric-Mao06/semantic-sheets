import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Braces, ListTree, Plus, X } from "lucide-react";
import type { ColumnInfo, Expr, Plan, Question, Step } from "@/types";
import { opLabel } from "@/lib/describe";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect, Textarea } from "@/components/ui/input";
import { Label, Notice } from "@/components/ui/misc";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";

export type Limits = { max_source_rows: number; max_provider_requests: number; spend_target_usd: number; deadline_seconds: number; rows_per_request?: number | null };

type Props = {
  plan: Plan;
  schema: ColumnInfo[];
  limits: Limits;
  onPlanChange: (p: Plan) => void;
  onLimitsChange: (l: Limits) => void;
};

const OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "icontains", "starts_with", "is_null", "not_null"] as const;

function simpleWhere(e: Expr): { column: string; op: string; literal: string | number | boolean | null } | null {
  if (!("op" in e)) return null;
  const a = e.args?.[0];
  if (!a || !("column" in a)) return null;
  if (e.op === "is_null" || e.op === "not_null") return { column: a.column, op: e.op, literal: null };
  const b = e.args?.[1];
  if (!b || !("literal" in b) || Array.isArray(b.literal)) return null;
  if (!(OPS as readonly string[]).includes(e.op)) return null;
  return { column: a.column, op: e.op, literal: b.literal };
}

function parseLiteral(s: string): string | number | boolean | null {
  const t = s.trim();
  if (t === "") return null;
  if (/^(true|false)$/i.test(t)) return /^true$/i.test(t);
  if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
  return t;
}

/** Full step-by-step editor. Lives behind “Details & edit” in the Operation panel. */
export default function PlanEditor({ plan, schema, limits, onPlanChange, onLimitsChange }: Props) {
  const [jsonMode, setJsonMode] = useState(false);
  const [jsonText, setJsonText] = useState("");
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  const jsonDirty = useRef(false);

  useEffect(() => {
    if (!jsonDirty.current) setJsonText(JSON.stringify(plan, null, 2));
  }, [plan]);

  const columnsByStep = useMemo(() => {
    // Columns available as input to each step (best effort, without running the compiler).
    const map: Record<string, string[]> = { source: schema.map((c) => c.name) };
    for (const s of plan.steps) {
      const inp = map[s.input] ?? [];
      let out = [...inp];
      if (s.op === "semantic_annotate") {
        for (const q of s.questions) {
          out.push(`${q.name}.value`, `${q.name}.score`, `${q.name}.status`);
          out.push(q.kind !== "boolean" ? `${q.name}.confidence` : `${q.name}.near`);
        }
      } else if (s.op === "compute") out.push(...s.columns.map((c) => c.name));
      else if (s.op === "aggregate") out = ["_row_id", ...s.group_by, ...s.metrics.map((m) => m.name)];
      else if (s.op === "project") out = ["_row_id", ...s.columns];
      else if (s.op === "join") out.push(...(s.right_columns ?? []).map((c) => (s.right_prefix ?? "right.") + c));
      else if (s.op === "semantic_match") {
        const n = s.name ?? "match";
        out.push(`${n}.right_row_id`, `${n}.score`, `${n}.status`, `${n}.candidates`, ...(s.right_output_columns ?? s.right_columns).map((c) => (s.right_prefix ?? "match.") + c));
      }
      map[s.id] = out;
    }
    return map;
  }, [plan, schema]);

  const update = (steps: Step[]) => onPlanChange({ ...plan, steps });
  const updateStep = (i: number, s: Step) => update(plan.steps.map((x, j) => (j === i ? s : x)));
  const outCols = columnsByStep[plan.output] ?? ["_row_id"];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <span className="label-mono">Steps</span>
        <span className="grow" />
        <Button variant="ghost" size="sm" onClick={() => setJsonMode((x) => !x)}>
          {jsonMode ? <ListTree /> : <Braces />}
          {jsonMode ? "Form" : "JSON"}
        </Button>
      </div>

      {jsonMode ? (
        <div className="flex flex-col gap-2">
          <Textarea
            className="min-h-64 font-mono text-[11.5px] leading-snug"
            spellCheck={false}
            value={jsonText}
            onChange={(e) => {
              jsonDirty.current = true;
              setJsonText(e.target.value);
              try {
                const p = JSON.parse(e.target.value) as Plan;
                setJsonErr(null);
                onPlanChange(p);
              } catch (err) {
                setJsonErr((err as Error).message);
              }
            }}
            onBlur={() => (jsonDirty.current = false)}
          />
          {jsonErr && <Notice tone="bad">{jsonErr}</Notice>}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {plan.steps.map((s, i) => (
            <StepEditor key={s.id + i} step={s} index={i} inputs={columnsByStep[s.input] ?? []} onChange={(ns) => updateStep(i, ns)} onRemove={() => update(plan.steps.filter((_, j) => j !== i))} isOutput={plan.output === s.id} onMakeOutput={() => onPlanChange({ ...plan, output: s.id })} />
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        <span className="label-mono mr-1">Add step</span>
        <Button variant="outline" size="sm" onClick={() => update([...plan.steps, { id: `sort_${plan.steps.length + 1}`, op: "sort", input: plan.output, by: [{ column: outCols[0], direction: "desc" }] }])}>
          <Plus /> Sort
        </Button>
        <Button variant="outline" size="sm" onClick={() => update([...plan.steps, { id: `filter_${plan.steps.length + 1}`, op: "filter", input: plan.output, where: { op: "not_null", args: [{ column: outCols[0] }] }, unknown_policy: "separate" }])}>
          <Plus /> Filter
        </Button>
        <Button variant="outline" size="sm" onClick={() => update([...plan.steps, { id: `group_${plan.steps.length + 1}`, op: "aggregate", input: plan.output, group_by: [outCols.find((c) => c.endsWith(".value")) ?? outCols[0]], metrics: [{ name: "n", fn: "count" }] }])}>
          <Plus /> Group
        </Button>
        <span className="grow" />
        <Label htmlFor="output-step">Result</Label>
        <NativeSelect id="output-step" className="w-auto" value={plan.output} onChange={(e) => onPlanChange({ ...plan, output: e.target.value })}>
          {plan.steps.map((s) => (
            <option key={s.id} value={s.id}>{s.id}</option>
          ))}
        </NativeSelect>
      </div>

      <div className="flex flex-col gap-2 border-t border-line pt-3">
        <span className="label-mono">Safety limits</span>
        <div className="grid grid-cols-2 gap-2 max-[900px]:grid-cols-1">
          <Field label="Max rows"><Input type="number" inputMode="decimal" value={limits.max_source_rows} onChange={(e) => onLimitsChange({ ...limits, max_source_rows: Number(e.target.value) })} /></Field>
          <Field label="Spend cap (USD)"><Input type="number" inputMode="decimal" step="0.05" value={limits.spend_target_usd} onChange={(e) => onLimitsChange({ ...limits, spend_target_usd: Number(e.target.value) })} /></Field>
          <Field label="Max model requests"><Input type="number" inputMode="decimal" value={limits.max_provider_requests} onChange={(e) => onLimitsChange({ ...limits, max_provider_requests: Number(e.target.value) })} /></Field>
          <Field label="Time limit (seconds)"><Input type="number" inputMode="decimal" value={limits.deadline_seconds} onChange={(e) => onLimitsChange({ ...limits, deadline_seconds: Number(e.target.value) })} /></Field>
          <Field label="Rows per request"><Input type="number" inputMode="decimal" min={1} max={50} value={limits.rows_per_request ?? 10} onChange={(e) => onLimitsChange({ ...limits, rows_per_request: Number(e.target.value) })} /></Field>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <label className={cn("flex flex-col gap-1", className)}>
      <span className="label-mono">{label}</span>
      {children}
    </label>
  );
}

function KV({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[100px_minmax(0,1fr)] items-center gap-x-3 gap-y-1.5 max-[900px]:grid-cols-1 max-[900px]:gap-y-1">
      <span className="label-mono">{label}</span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function Chips({ items, onRemove, options, onAdd }: { items: string[]; onRemove: (c: string) => void; options: string[]; onAdd: (c: string) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {items.map((c) => (
        <span key={c} className="inline-flex items-center gap-1 rounded-[4px] border border-line bg-field px-1.5 py-px font-mono text-[11.5px] text-ink">
          {c}
          <button type="button" className="text-ink-tertiary hover:text-ink" onClick={() => onRemove(c)} aria-label={`Remove ${c}`}>
            <X className="size-3" />
          </button>
        </span>
      ))}
      <NativeSelect className="w-auto [&>select]:h-6 [&>select]:text-[11.5px]" value="" onChange={(e) => e.target.value && onAdd(e.target.value)}>
        <option value="">+ add</option>
        {options.map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </NativeSelect>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------

function StepEditor({ step, index, inputs, onChange, onRemove, isOutput, onMakeOutput }: { step: Step; index: number; inputs: string[]; onChange: (s: Step) => void; onRemove: () => void; isOutput: boolean; onMakeOutput: () => void }) {
  const [raw, setRaw] = useState<string | null>(null);
  return (
    <div className="grain flex flex-col gap-2.5 rounded-md border border-line bg-paper p-3">
      <header className="flex items-center gap-2 text-[12.5px]">
        <span className="inline-flex size-5 shrink-0 items-center justify-center rounded-full bg-ink font-mono text-[10.5px] text-white">{index + 1}</span>
        <span className="shrink-0 font-medium text-ink">{opLabel(step.op)}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-tertiary" title={`${step.id} ← ${step.input}`}>{step.id} ← {step.input}</span>
        {isOutput ? <Badge variant="ink">result</Badge> : <Button variant="ghost" size="sm" onClick={onMakeOutput}>use as result</Button>}
        <Button variant="ghost" size="icon-sm" onClick={onRemove} aria-label="Remove step"><X /></Button>
      </header>

      {step.op === "semantic_annotate" && (
        <>
          <KV label="Read from">
            <Chips items={step.columns} onRemove={(c) => onChange({ ...step, columns: step.columns.filter((x) => x !== c) })} options={inputs.filter((c) => !step.columns.includes(c) && c !== "_row_id")} onAdd={(c) => onChange({ ...step, columns: [...step.columns, c] })} />
          </KV>
          <KV label="If text missing">
            <NativeSelect className="w-auto" value={step.on_missing ?? "unknown"} onChange={(e) => onChange({ ...step, on_missing: e.target.value as "unknown" | "skip" })}>
              <option value="unknown">mark as missing</option>
              <option value="skip">skip row</option>
            </NativeSelect>
          </KV>
          {step.questions.map((q, qi) => (
            <QuestionEditor key={qi} q={q} onChange={(nq) => onChange({ ...step, questions: step.questions.map((x, j) => (j === qi ? nq : x)) })} onRemove={() => onChange({ ...step, questions: step.questions.filter((_, j) => j !== qi) })} />
          ))}
          <div className="flex flex-wrap gap-1.5">
            <Button variant="outline" size="sm" onClick={() => onChange({ ...step, questions: [...step.questions, { name: `flag_${step.questions.length + 1}`, kind: "boolean", instruction: "Does the text …?", thresholds: { true_min: 0.7, false_max: 0.3 } }] })}><Plus /> Yes / no</Button>
            <Button variant="outline" size="sm" onClick={() => onChange({ ...step, questions: [...step.questions, { name: `label_${step.questions.length + 1}`, kind: "category", instruction: "Which category best describes the text?", options: { a: "…", b: "…", other: "none of the above" } }] })}><Plus /> Category</Button>
            <Button variant="outline" size="sm" onClick={() => onChange({ ...step, questions: [...step.questions, { name: `score_${step.questions.length + 1}`, kind: "score", instruction: "How severe is …?", levels: ["None", "Minor", "Major", "Critical"] }] })}><Plus /> Score</Button>
          </div>
        </>
      )}

      {step.op === "filter" && (() => {
        const simple = simpleWhere(step.where);
        return (
          <>
            <KV label="Keep rows where">
              {simple && raw === null ? (
                <div className="flex flex-wrap items-center gap-1.5">
                  <NativeSelect className="min-w-0 flex-1 basis-32" value={simple.column} onChange={(e) => onChange({ ...step, where: { op: simple.op, args: [{ column: e.target.value }, ...(simple.op.endsWith("null") ? [] : [{ literal: simple.literal }])] } })}>
                    {[...new Set([simple.column, ...inputs])].map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </NativeSelect>
                  <NativeSelect className="w-auto" value={simple.op} onChange={(e) => onChange({ ...step, where: { op: e.target.value, args: [{ column: simple.column }, ...(e.target.value.endsWith("null") ? [] : [{ literal: simple.literal ?? "" }])] } })}>
                    {OPS.map((o) => (
                      <option key={o} value={o}>{o}</option>
                    ))}
                  </NativeSelect>
                  {!simple.op.endsWith("null") && (
                    <Input className="min-w-0 flex-1 basis-24" type="text" value={simple.literal === null ? "" : String(simple.literal)} onChange={(e) => onChange({ ...step, where: { op: simple.op, args: [{ column: simple.column }, { literal: parseLiteral(e.target.value) }] } })} />
                  )}
                </div>
              ) : (
                <Textarea
                  className="font-mono text-[11.5px]"
                  value={raw ?? JSON.stringify(step.where)}
                  onChange={(e) => {
                    setRaw(e.target.value);
                    try {
                      onChange({ ...step, where: JSON.parse(e.target.value) as Expr });
                    } catch {
                      /* keep typing */
                    }
                  }}
                  onBlur={() => setRaw(null)}
                />
              )}
            </KV>
            <KV label="Unsure rows">
              <NativeSelect className="w-auto" value={step.unknown_policy ?? "separate"} onChange={(e) => onChange({ ...step, unknown_policy: e.target.value as "separate" | "exclude" | "include" })}>
                <option value="separate">set aside for review</option>
                <option value="exclude">leave out</option>
                <option value="include">treat as-is</option>
              </NativeSelect>
            </KV>
          </>
        );
      })()}

      {step.op === "sort" && (
        <KV label="Order by">
          <div className="flex flex-col gap-1.5">
            {step.by.map((k, ki) => (
              <div className="flex flex-wrap items-center gap-1.5" key={ki}>
                <NativeSelect className="min-w-0 flex-1 basis-32" value={k.column} onChange={(e) => onChange({ ...step, by: step.by.map((x, j) => (j === ki ? { ...x, column: e.target.value } : x)) })}>
                  {[...new Set([k.column, ...inputs])].map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </NativeSelect>
                <NativeSelect className="w-auto" value={k.direction} onChange={(e) => onChange({ ...step, by: step.by.map((x, j) => (j === ki ? { ...x, direction: e.target.value as "asc" | "desc" } : x)) })}>
                  <option value="desc">highest first</option>
                  <option value="asc">lowest first</option>
                </NativeSelect>
                {step.by.length > 1 && <Button variant="ghost" size="icon-sm" onClick={() => onChange({ ...step, by: step.by.filter((_, j) => j !== ki) })} aria-label="Remove key"><X /></Button>}
              </div>
            ))}
            <Button variant="outline" size="sm" className="self-start" onClick={() => onChange({ ...step, by: [...step.by, { column: inputs[0] ?? "_row_id", direction: "asc" }] })}><Plus /> Key</Button>
          </div>
        </KV>
      )}

      {step.op === "aggregate" && (
        <>
          <KV label="Group by">
            <Chips items={step.group_by} onRemove={(c) => onChange({ ...step, group_by: step.group_by.filter((x) => x !== c) })} options={inputs.filter((c) => !step.group_by.includes(c))} onAdd={(c) => onChange({ ...step, group_by: [...step.group_by, c] })} />
          </KV>
          <KV label="Metrics">
            <div className="flex flex-col gap-1.5">
              {step.metrics.map((m, mi) => (
                <div className="flex flex-wrap items-center gap-1.5" key={mi}>
                  <Input type="text" className="w-24" value={m.name} onChange={(e) => onChange({ ...step, metrics: step.metrics.map((x, j) => (j === mi ? { ...x, name: e.target.value } : x)) })} />
                  <NativeSelect className="w-auto" value={m.fn} onChange={(e) => onChange({ ...step, metrics: step.metrics.map((x, j) => (j === mi ? { ...x, fn: e.target.value } : x)) })}>
                    {["count", "count_distinct", "sum", "avg", "min", "max"].map((f) => (
                      <option key={f} value={f}>{f}</option>
                    ))}
                  </NativeSelect>
                  {m.fn !== "count" || m.column ? (
                    <NativeSelect className="min-w-0 flex-1 basis-28" value={m.column ?? ""} onChange={(e) => onChange({ ...step, metrics: step.metrics.map((x, j) => (j === mi ? { ...x, column: e.target.value || null } : x)) })}>
                      <option value="">(all rows)</option>
                      {inputs.map((c) => (
                        <option key={c} value={c}>{c}</option>
                      ))}
                    </NativeSelect>
                  ) : (
                    <span className="text-[12px] text-ink-muted">all rows</span>
                  )}
                  <Button variant="ghost" size="icon-sm" onClick={() => onChange({ ...step, metrics: step.metrics.filter((_, j) => j !== mi) })} aria-label="Remove metric"><X /></Button>
                </div>
              ))}
              <Button variant="outline" size="sm" className="self-start" onClick={() => onChange({ ...step, metrics: [...step.metrics, { name: `m${step.metrics.length + 1}`, fn: "count" }] })}><Plus /> Metric</Button>
            </div>
          </KV>
        </>
      )}

      {step.op === "limit" && (
        <KV label="Rows"><Input type="number" inputMode="decimal" className="w-28" value={step.n} onChange={(e) => onChange({ ...step, n: Number(e.target.value) })} /></KV>
      )}

      {step.op === "semantic_match" && (
        <>
          <KV label="Relation"><Textarea value={step.instruction} onChange={(e) => onChange({ ...step, instruction: e.target.value })} /></KV>
          <KV label="This table"><span className="font-mono text-[11.5px]">{step.left_columns.join(", ")}</span></KV>
          <KV label="Other table"><span className="font-mono text-[11.5px]">{step.right_columns.join(", ")}</span></KV>
          <KV label="Candidates / row"><Input type="number" inputMode="decimal" className="w-24" min={1} max={5} value={step.candidates_per_row ?? 5} onChange={(e) => onChange({ ...step, candidates_per_row: Number(e.target.value) })} /></KV>
          <KV label="Accept ≥"><Input type="number" inputMode="decimal" className="w-24" step={0.05} min={0} max={1} value={step.accept_min ?? 0.8} onChange={(e) => onChange({ ...step, accept_min: Number(e.target.value) })} /></KV>
          <KV label="Reject ≤"><Input type="number" inputMode="decimal" className="w-24" step={0.05} min={0} max={1} value={step.reject_max ?? 0.3} onChange={(e) => onChange({ ...step, reject_max: Number(e.target.value) })} /></KV>
        </>
      )}

      {(step.op === "project" || step.op === "compute" || step.op === "join" || step.op === "distinct") && (
        <Textarea
          className="font-mono text-[11.5px]"
          spellCheck={false}
          defaultValue={JSON.stringify(step, null, 1)}
          onBlur={(e) => {
            try {
              onChange(JSON.parse(e.target.value) as Step);
            } catch {
              /* ignore */
            }
          }}
        />
      )}
    </div>
  );
}

function QuestionEditor({ q, onChange, onRemove }: { q: Question; onChange: (q: Question) => void; onRemove: () => void }) {
  const auto = (q.thresholds?.mode ?? "auto") === "auto";
  const tmin = q.thresholds?.true_min ?? 0.85;
  const fmax = q.thresholds?.false_max ?? 0.15;
  return (
    <div className="flex flex-col gap-2 rounded-md border-l-2 border-ink/70 bg-field/60 px-3 py-2.5">
      <div className="flex items-center gap-2">
        <Input type="text" className="min-w-0 flex-1 font-mono text-[12px]" value={q.name} onChange={(e) => onChange({ ...q, name: e.target.value.replace(/[^A-Za-z0-9_]/g, "_") })} />
        <Badge className="shrink-0">{q.kind === "boolean" ? "yes / no" : q.kind}</Badge>
        <Button variant="ghost" size="icon-sm" onClick={onRemove} aria-label="Remove question"><X /></Button>
      </div>
      <Textarea value={q.instruction} onChange={(e) => onChange({ ...q, instruction: e.target.value })} />

      {q.kind === "boolean" && (
        <div className="flex flex-col gap-2">
          <label className="flex items-center gap-2 text-[12px] text-ink-muted">
            <Switch checked={auto} onCheckedChange={(v) => onChange({ ...q, thresholds: { true_min: tmin, false_max: fmax, mode: v ? "auto" : "fixed" } })} />
            Let the model pick the yes/no cut from the scores it produces
          </label>
          <KV label={`${auto ? "Fallback " : ""}yes ≥ ${tmin.toFixed(2)}`}>
            <Slider min={0} max={1} step={0.01} value={[tmin]} onValueChange={([v]) => onChange({ ...q, thresholds: { ...q.thresholds, true_min: v, false_max: Math.min(fmax, v), mode: q.thresholds?.mode } })} />
          </KV>
          <KV label={`${auto ? "Fallback " : ""}no ≤ ${fmax.toFixed(2)}`}>
            <Slider min={0} max={1} step={0.01} value={[fmax]} onValueChange={([v]) => onChange({ ...q, thresholds: { ...q.thresholds, true_min: Math.max(tmin, v), false_max: v, mode: q.thresholds?.mode } })} />
          </KV>
          {q.criteria && (
            <>
              <KV label="Yes means"><Input type="text" value={q.criteria.true ?? ""} onChange={(e) => onChange({ ...q, criteria: { ...q.criteria, true: e.target.value } })} /></KV>
              <KV label="No means"><Input type="text" value={q.criteria.false ?? ""} onChange={(e) => onChange({ ...q, criteria: { ...q.criteria, false: e.target.value } })} /></KV>
            </>
          )}
        </div>
      )}

      {q.kind === "category" && (
        <div className="flex flex-col gap-1.5">
          {Object.entries(q.options ?? {}).map(([label, desc]) => (
            <div className="grid grid-cols-[7.5rem_minmax(0,1fr)_auto] items-center gap-1.5 max-[900px]:grid-cols-[minmax(0,1fr)_auto]" key={label}>
              <Input
                type="text"
                className="font-mono text-[12px]"
                value={label}
                onChange={(e) => {
                  const entries = Object.entries(q.options ?? {}).map(([k, v]) => (k === label ? [e.target.value.replace(/[^A-Za-z0-9_ -]/g, "_"), v] : [k, v]));
                  onChange({ ...q, options: Object.fromEntries(entries) });
                }}
              />
              <Input type="text" className="min-w-0 max-[900px]:col-span-2 max-[900px]:row-start-2" value={desc ?? ""} placeholder="what this label means" onChange={(e) => onChange({ ...q, options: { ...q.options, [label]: e.target.value } })} />
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`Remove ${label}`}
                onClick={() => {
                  const o = { ...q.options };
                  delete o[label];
                  onChange({ ...q, options: o });
                }}
              >
                <X />
              </Button>
            </div>
          ))}
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => onChange({ ...q, options: { ...q.options, [`label_${Object.keys(q.options ?? {}).length + 1}`]: "" } })}><Plus /> Label</Button>
            <span className="label-mono">min confidence</span>
            <Input type="number" inputMode="decimal" step={0.05} min={0} max={1} value={q.min_confidence ?? 0} className="w-20" onChange={(e) => onChange({ ...q, min_confidence: Number(e.target.value) })} />
          </div>
        </div>
      )}

      {q.kind === "score" && (
        <div className="flex flex-col gap-1.5">
          {(q.levels ?? []).map((l, li) => (
            <div className="flex items-center gap-1.5" key={li}>
              <Badge className="w-7 justify-center">{li}</Badge>
              <Input type="text" value={l} onChange={(e) => onChange({ ...q, levels: (q.levels ?? []).map((x, j) => (j === li ? e.target.value : x)) })} />
              <Button variant="ghost" size="icon-sm" onClick={() => onChange({ ...q, levels: (q.levels ?? []).filter((_, j) => j !== li) })} aria-label="Remove level"><X /></Button>
            </div>
          ))}
          <Button variant="outline" size="sm" className="self-start" onClick={() => onChange({ ...q, levels: [...(q.levels ?? []), "…"] })}><Plus /> Level</Button>
        </div>
      )}
    </div>
  );
}
