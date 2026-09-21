import { useEffect, useMemo, useRef, useState } from "react";
import type { ColumnInfo, Estimate, Expr, Job, Plan, Question, Step, ValidateResponse } from "../types";

export type Limits = { max_source_rows: number; max_provider_requests: number; spend_target_usd: number; deadline_seconds: number; rows_per_request?: number | null };

type Props = {
  plan: Plan | null;
  validation: ValidateResponse | null;
  validating: boolean;
  validationError: string | null;
  schema: ColumnInfo[];
  limits: Limits;
  job: Job | null;
  budgetLeftUsd: number;
  onPlanChange: (p: Plan) => void;
  onLimitsChange: (l: Limits) => void;
  onRun: () => void;
  onCancel: () => void;
  onClear: () => void;
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

export default function PlanPanel({ plan, validation, validating, validationError, schema, limits, job, budgetLeftUsd, onPlanChange, onLimitsChange, onRun, onCancel, onClear }: Props) {
  const [jsonMode, setJsonMode] = useState(false);
  const [jsonText, setJsonText] = useState("");
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  const jsonDirty = useRef(false);

  useEffect(() => {
    if (!jsonDirty.current) setJsonText(plan ? JSON.stringify(plan, null, 2) : "");
  }, [plan]);

  const columnsByStep = useMemo(() => {
    // Columns available as input to each step (best effort, without running the compiler).
    const map: Record<string, string[]> = { source: schema.map((c) => c.name) };
    if (!plan) return map;
    for (const s of plan.steps) {
      const inp = map[s.input] ?? [];
      let out = [...inp];
      if (s.op === "semantic_annotate") {
        for (const q of s.questions) {
          out.push(`${q.name}.value`, `${q.name}.score`, `${q.name}.status`);
          if (q.kind !== "boolean") out.push(`${q.name}.confidence`);
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

  if (!plan) {
    return (
      <div className="body">
        <div className="muted" style={{ lineHeight: 1.5 }}>
          Describe an operation in the command bar, for example <i>“Add issue type and severity, then show serious onboarding complaints.”</i>
          <br />
          <br />
          The planner ({validation?.planner?.model ?? "frontier model"}) reads only the schema and a bounded sample, then proposes editable steps. Nothing runs until you press <b>Run</b>.
        </div>
      </div>
    );
  }

  const update = (steps: Step[]) => onPlanChange({ ...plan, steps });
  const updateStep = (i: number, s: Step) => update(plan.steps.map((x, j) => (j === i ? s : x)));
  const running = job && (job.state === "queued" || job.state === "running");
  const est: Estimate | undefined = validation?.estimate;
  const overBudget = est ? est.estimated_cost_usd > budgetLeftUsd : false;

  return (
    <div className="body">
      <div className="row">
        <div className="grow">
          <h3>{plan.title ?? "Operation plan"}</h3>
          {plan.description && <div className="muted" style={{ fontSize: 12.5, marginTop: 2 }}>{plan.description}</div>}
        </div>
        <button className="btn small ghost" onClick={() => setJsonMode((x) => !x)}>{jsonMode ? "Steps" : "JSON"}</button>
        <button className="btn small ghost" onClick={onClear} title="Discard plan">✕</button>
      </div>

      {jsonMode ? (
        <div>
          <textarea
            className="plan-json"
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
          {jsonErr && <div className="error">{jsonErr}</div>}
        </div>
      ) : (
        plan.steps.map((s, i) => (
          <StepEditor key={s.id + i} step={s} index={i} inputs={columnsByStep[s.input] ?? []} onChange={(ns) => updateStep(i, ns)} onRemove={() => update(plan.steps.filter((_, j) => j !== i))} isOutput={plan.output === s.id} onMakeOutput={() => onPlanChange({ ...plan, output: s.id })} />
        ))
      )}

      <div className="row wrap">
        <span className="muted">Output step:</span>
        <select value={plan.output} onChange={(e) => onPlanChange({ ...plan, output: e.target.value })}>
          {plan.steps.map((s) => (
            <option key={s.id} value={s.id}>{s.id}</option>
          ))}
        </select>
        <span className="grow" />
        <button className="btn small" onClick={() => update([...plan.steps, { id: `sort_${plan.steps.length + 1}`, op: "sort", input: plan.output, by: [{ column: (columnsByStep[plan.output] ?? ["_row_id"])[0], direction: "desc" }] }])}>+ sort</button>
        <button className="btn small" onClick={() => update([...plan.steps, { id: `filter_${plan.steps.length + 1}`, op: "filter", input: plan.output, where: { op: "not_null", args: [{ column: (columnsByStep[plan.output] ?? ["_row_id"])[0] }] }, unknown_policy: "separate" }])}>+ filter</button>
        <button className="btn small" onClick={() => update([...plan.steps, { id: `group_${plan.steps.length + 1}`, op: "aggregate", input: plan.output, group_by: [(columnsByStep[plan.output] ?? []).find((c) => c.endsWith(".value")) ?? (columnsByStep[plan.output] ?? ["_row_id"])[0]], metrics: [{ name: "n", fn: "count" }] }])}>+ group</button>
      </div>

      <div>
        <div className="row" style={{ marginBottom: 6 }}>
          <b>Estimate</b> {validating && <span className="spinner" />}
          {validation && <span className="badge mono" title="plan hash">{validation.plan_hash.slice(0, 10)}</span>}
        </div>
        {validationError && <div className="error">{validationError}</div>}
        {est && (
          <div className="estimate">
            <div className="cell"><b>{est.semantic_rows.toLocaleString()}</b><span>rows to judge</span></div>
            <div className="cell"><b>{est.provider_requests.toLocaleString()}</b><span>Jev requests</span></div>
            <div className="cell"><b>${est.estimated_cost_usd.toFixed(4)}</b><span>{(est.input_tokens / 1000).toFixed(0)}k input tokens</span></div>
            <div className="cell"><b>{est.inference_attempts.toLocaleString()}</b><span>judgements</span></div>
            <div className="cell"><b>{est.quota_floor_seconds < 60 ? `${est.quota_floor_seconds.toFixed(0)}s` : `${(est.quota_floor_seconds / 60).toFixed(1)}m`}</b><span>quota floor</span></div>
            <div className="cell"><b>{est.candidate_pairs.toLocaleString()}</b><span>candidate pairs</span></div>
          </div>
        )}
        {validation?.warnings?.length ? (
          <div className="warnings" style={{ marginTop: 6 }}>
            {validation.warnings.map((w, i) => (
              <div key={i}>⚠ {w}</div>
            ))}
          </div>
        ) : null}
      </div>

      <details>
        <summary>Job limits (preauthorized budget guard)</summary>
        <div className="limits" style={{ marginTop: 6 }}>
          <label>max source rows<input type="number" value={limits.max_source_rows} onChange={(e) => onLimitsChange({ ...limits, max_source_rows: Number(e.target.value) })} /></label>
          <label>spend target (USD)<input type="number" step="0.05" value={limits.spend_target_usd} onChange={(e) => onLimitsChange({ ...limits, spend_target_usd: Number(e.target.value) })} /></label>
          <label>max provider requests<input type="number" value={limits.max_provider_requests} onChange={(e) => onLimitsChange({ ...limits, max_provider_requests: Number(e.target.value) })} /></label>
          <label>deadline (seconds)<input type="number" value={limits.deadline_seconds} onChange={(e) => onLimitsChange({ ...limits, deadline_seconds: Number(e.target.value) })} /></label>
          <label>rows per Jev request<input type="number" min={1} max={50} value={limits.rows_per_request ?? 10} onChange={(e) => onLimitsChange({ ...limits, rows_per_request: Number(e.target.value) })} /></label>
        </div>
      </details>

      <div className="row">
        {running ? (
          <button className="btn danger" onClick={onCancel}>Cancel job</button>
        ) : (
          <button className="btn primary" disabled={!validation || validating || !!validationError} onClick={onRun} title={overBudget ? "Estimate exceeds remaining workspace budget" : ""}>
            {overBudget ? "Run (over budget)" : job ? "Run again" : "Run"}
          </button>
        )}
        <span className="muted" style={{ fontSize: 12 }}>
          {job ? `job ${job.job_id.slice(-6)} · ${job.state}${job.terminal_reason ? ` (${job.terminal_reason})` : ""}` : "no provider calls until you run"}
        </span>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------

function StepEditor({ step, index, inputs, onChange, onRemove, isOutput, onMakeOutput }: { step: Step; index: number; inputs: string[]; onChange: (s: Step) => void; onRemove: () => void; isOutput: boolean; onMakeOutput: () => void }) {
  const [raw, setRaw] = useState<string | null>(null);
  return (
    <div className="step">
      <header>
        <span className="badge accent">{index + 1}</span>
        <span className="id">{step.id}</span>
        <span className="muted">{step.op}</span>
        <span className="muted" style={{ fontSize: 11.5 }}>← {step.input}</span>
        <span className="grow" />
        {isOutput ? <span className="badge ok">output</span> : <button className="btn small ghost" onClick={onMakeOutput}>make output</button>}
        <button className="btn small ghost" onClick={onRemove} title="Remove step">✕</button>
      </header>
      {step.op === "semantic_annotate" && (
        <>
          <div className="kv">
            <label>input columns</label>
            <div className="chips">
              {step.columns.map((c) => (
                <span className="chip" key={c}>
                  {c}
                  <button onClick={() => onChange({ ...step, columns: step.columns.filter((x) => x !== c) })}>×</button>
                </span>
              ))}
              <select value="" onChange={(e) => e.target.value && onChange({ ...step, columns: [...step.columns, e.target.value] })} style={{ width: "auto" }}>
                <option value="">+ add</option>
                {inputs.filter((c) => !step.columns.includes(c) && c !== "_row_id").map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </div>
            <label>missing input</label>
            <select value={step.on_missing ?? "unknown"} onChange={(e) => onChange({ ...step, on_missing: e.target.value as "unknown" | "skip" })} style={{ width: "auto" }}>
              <option value="unknown">mark as missing</option>
              <option value="skip">skip row</option>
            </select>
          </div>
          {step.questions.map((q, qi) => (
            <QuestionEditor key={qi} q={q} onChange={(nq) => onChange({ ...step, questions: step.questions.map((x, j) => (j === qi ? nq : x)) })} onRemove={() => onChange({ ...step, questions: step.questions.filter((_, j) => j !== qi) })} />
          ))}
          <div className="row">
            <button className="btn small" onClick={() => onChange({ ...step, questions: [...step.questions, { name: `flag_${step.questions.length + 1}`, kind: "boolean", instruction: "Does the text …?", thresholds: { true_min: 0.7, false_max: 0.3 } }] })}>+ yes/no</button>
            <button className="btn small" onClick={() => onChange({ ...step, questions: [...step.questions, { name: `label_${step.questions.length + 1}`, kind: "category", instruction: "Which category best describes the text?", options: { a: "…", b: "…", other: "none of the above" } }] })}>+ category</button>
            <button className="btn small" onClick={() => onChange({ ...step, questions: [...step.questions, { name: `score_${step.questions.length + 1}`, kind: "score", instruction: "How severe is …?", levels: ["None", "Minor", "Major", "Critical"] }] })}>+ score</button>
          </div>
        </>
      )}
      {step.op === "filter" && (() => {
        const simple = simpleWhere(step.where);
        return (
          <div className="kv">
            <label>keep rows where</label>
            {simple && raw === null ? (
              <div className="row">
                <select value={simple.column} onChange={(e) => onChange({ ...step, where: { op: simple.op, args: [{ column: e.target.value }, ...(simple.op.endsWith("null") ? [] : [{ literal: simple.literal }])] } })}>
                  {[...new Set([simple.column, ...inputs])].map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
                <select value={simple.op} onChange={(e) => onChange({ ...step, where: { op: e.target.value, args: [{ column: simple.column }, ...(e.target.value.endsWith("null") ? [] : [{ literal: simple.literal ?? "" }])] } })} style={{ width: "auto" }}>
                  {OPS.map((o) => (
                    <option key={o} value={o}>{o}</option>
                  ))}
                </select>
                {!simple.op.endsWith("null") && (
                  <input type="text" value={simple.literal === null ? "" : String(simple.literal)} onChange={(e) => onChange({ ...step, where: { op: simple.op, args: [{ column: simple.column }, { literal: parseLiteral(e.target.value) }] } })} />
                )}
              </div>
            ) : (
              <textarea value={raw ?? JSON.stringify(step.where)} onChange={(e) => {
                setRaw(e.target.value);
                try {
                  onChange({ ...step, where: JSON.parse(e.target.value) as Expr });
                } catch {
                  /* keep typing */
                }
              }} onBlur={() => setRaw(null)} />
            )}
            <label>unknown answers</label>
            <select value={step.unknown_policy ?? "separate"} onChange={(e) => onChange({ ...step, unknown_policy: e.target.value as "separate" | "exclude" | "include" })} style={{ width: "auto" }}>
              <option value="separate">separate into review view</option>
              <option value="exclude">exclude silently</option>
              <option value="include">evaluate as-is</option>
            </select>
          </div>
        );
      })()}
      {step.op === "sort" && (
        <div className="kv">
          <label>order by</label>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {step.by.map((k, ki) => (
              <div className="row" key={ki}>
                <select value={k.column} onChange={(e) => onChange({ ...step, by: step.by.map((x, j) => (j === ki ? { ...x, column: e.target.value } : x)) })}>
                  {[...new Set([k.column, ...inputs])].map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
                <select value={k.direction} onChange={(e) => onChange({ ...step, by: step.by.map((x, j) => (j === ki ? { ...x, direction: e.target.value as "asc" | "desc" } : x)) })} style={{ width: "auto" }}>
                  <option value="desc">desc</option>
                  <option value="asc">asc</option>
                </select>
                {step.by.length > 1 && <button className="btn small ghost" onClick={() => onChange({ ...step, by: step.by.filter((_, j) => j !== ki) })}>×</button>}
              </div>
            ))}
            <button className="btn small" style={{ alignSelf: "flex-start" }} onClick={() => onChange({ ...step, by: [...step.by, { column: inputs[0] ?? "_row_id", direction: "asc" }] })}>+ key</button>
          </div>
        </div>
      )}
      {step.op === "aggregate" && (
        <div className="kv">
          <label>group by</label>
          <div className="chips">
            {step.group_by.map((c) => (
              <span className="chip" key={c}>{c}<button onClick={() => onChange({ ...step, group_by: step.group_by.filter((x) => x !== c) })}>×</button></span>
            ))}
            <select value="" onChange={(e) => e.target.value && onChange({ ...step, group_by: [...step.group_by, e.target.value] })} style={{ width: "auto" }}>
              <option value="">+ add</option>
              {inputs.filter((c) => !step.group_by.includes(c)).map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <label>metrics</label>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {step.metrics.map((m, mi) => (
              <div className="row" key={mi}>
                <input type="text" value={m.name} style={{ width: 90 }} onChange={(e) => onChange({ ...step, metrics: step.metrics.map((x, j) => (j === mi ? { ...x, name: e.target.value } : x)) })} />
                <select value={m.fn} onChange={(e) => onChange({ ...step, metrics: step.metrics.map((x, j) => (j === mi ? { ...x, fn: e.target.value } : x)) })} style={{ width: "auto" }}>
                  {["count", "count_distinct", "sum", "avg", "min", "max"].map((f) => (
                    <option key={f} value={f}>{f}</option>
                  ))}
                </select>
                {m.fn !== "count" || m.column ? (
                  <select value={m.column ?? ""} onChange={(e) => onChange({ ...step, metrics: step.metrics.map((x, j) => (j === mi ? { ...x, column: e.target.value || null } : x)) })}>
                    <option value="">(all rows)</option>
                    {inputs.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                ) : (
                  <span className="muted">all rows</span>
                )}
                <button className="btn small ghost" onClick={() => onChange({ ...step, metrics: step.metrics.filter((_, j) => j !== mi) })}>×</button>
              </div>
            ))}
            <button className="btn small" style={{ alignSelf: "flex-start" }} onClick={() => onChange({ ...step, metrics: [...step.metrics, { name: `m${step.metrics.length + 1}`, fn: "count" }] })}>+ metric</button>
          </div>
        </div>
      )}
      {step.op === "limit" && (
        <div className="kv"><label>rows</label><input type="number" value={step.n} onChange={(e) => onChange({ ...step, n: Number(e.target.value) })} /></div>
      )}
      {step.op === "semantic_match" && (
        <div className="kv">
          <label>relation</label>
          <textarea value={step.instruction} onChange={(e) => onChange({ ...step, instruction: e.target.value })} />
          <label>left columns</label><div className="mono">{step.left_columns.join(", ")}</div>
          <label>right columns</label><div className="mono">{step.right_columns.join(", ")}</div>
          <label>candidates / row</label><input type="number" min={1} max={5} value={step.candidates_per_row ?? 5} onChange={(e) => onChange({ ...step, candidates_per_row: Number(e.target.value) })} />
          <label>accept ≥</label><input type="number" step={0.05} min={0} max={1} value={step.accept_min ?? 0.8} onChange={(e) => onChange({ ...step, accept_min: Number(e.target.value) })} />
          <label>reject ≤</label><input type="number" step={0.05} min={0} max={1} value={step.reject_max ?? 0.3} onChange={(e) => onChange({ ...step, reject_max: Number(e.target.value) })} />
        </div>
      )}
      {(step.op === "project" || step.op === "compute" || step.op === "join" || step.op === "distinct") && (
        <textarea
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
  return (
    <div className="question">
      <div className="row">
        <input type="text" className="name" value={q.name} style={{ width: 140 }} onChange={(e) => onChange({ ...q, name: e.target.value.replace(/[^A-Za-z0-9_]/g, "_") })} />
        <span className="badge">{q.kind === "boolean" ? "yes / no" : q.kind}</span>
        <span className="grow" />
        <button className="btn small ghost" onClick={onRemove}>×</button>
      </div>
      <textarea value={q.instruction} onChange={(e) => onChange({ ...q, instruction: e.target.value })} />
      {q.kind === "boolean" && (
        <div className="kv">
          <label>true when p ≥ {q.thresholds?.true_min ?? 0.85}</label>
          <input type="range" min={0} max={1} step={0.01} value={q.thresholds?.true_min ?? 0.85} onChange={(e) => onChange({ ...q, thresholds: { true_min: Number(e.target.value), false_max: Math.min(q.thresholds?.false_max ?? 0.15, Number(e.target.value)) } })} />
          <label>false when p ≤ {q.thresholds?.false_max ?? 0.15}</label>
          <input type="range" min={0} max={1} step={0.01} value={q.thresholds?.false_max ?? 0.15} onChange={(e) => onChange({ ...q, thresholds: { true_min: Math.max(q.thresholds?.true_min ?? 0.85, Number(e.target.value)), false_max: Number(e.target.value) } })} />
          {q.criteria && (
            <>
              <label>yes means</label><input type="text" value={q.criteria.true ?? ""} onChange={(e) => onChange({ ...q, criteria: { ...q.criteria, true: e.target.value } })} />
              <label>no means</label><input type="text" value={q.criteria.false ?? ""} onChange={(e) => onChange({ ...q, criteria: { ...q.criteria, false: e.target.value } })} />
            </>
          )}
        </div>
      )}
      {q.kind === "category" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {Object.entries(q.options ?? {}).map(([label, desc]) => (
            <div className="row" key={label}>
              <input type="text" value={label} style={{ width: 130 }} onChange={(e) => {
                const entries = Object.entries(q.options ?? {}).map(([k, v]) => (k === label ? [e.target.value.replace(/[^A-Za-z0-9_ -]/g, "_"), v] : [k, v]));
                onChange({ ...q, options: Object.fromEntries(entries) });
              }} />
              <input type="text" value={desc ?? ""} placeholder="description" onChange={(e) => onChange({ ...q, options: { ...q.options, [label]: e.target.value } })} />
              <button className="btn small ghost" onClick={() => {
                const o = { ...q.options };
                delete o[label];
                onChange({ ...q, options: o });
              }}>×</button>
            </div>
          ))}
          <div className="row">
            <button className="btn small" onClick={() => onChange({ ...q, options: { ...q.options, [`label_${Object.keys(q.options ?? {}).length + 1}`]: "" } })}>+ label</button>
            <span className="muted" style={{ fontSize: 12 }}>min confidence</span>
            <input type="number" step={0.05} min={0} max={1} value={q.min_confidence ?? 0} style={{ width: 70 }} onChange={(e) => onChange({ ...q, min_confidence: Number(e.target.value) })} />
          </div>
        </div>
      )}
      {q.kind === "score" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {(q.levels ?? []).map((l, li) => (
            <div className="row" key={li}>
              <span className="badge">{li}</span>
              <input type="text" value={l} onChange={(e) => onChange({ ...q, levels: (q.levels ?? []).map((x, j) => (j === li ? e.target.value : x)) })} />
              <button className="btn small ghost" onClick={() => onChange({ ...q, levels: (q.levels ?? []).filter((_, j) => j !== li) })}>×</button>
            </div>
          ))}
          <button className="btn small" style={{ alignSelf: "flex-start" }} onClick={() => onChange({ ...q, levels: [...(q.levels ?? []), "…"] })}>+ level</button>
        </div>
      )}
    </div>
  );
}
