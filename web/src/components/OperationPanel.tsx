import type { Plan, PlanDescription, Question, Step } from "../types";

interface Props {
  plan: Plan | null;
  desc: PlanDescription | null;
  dirty: boolean;
  busy: boolean;
  spendTarget: number;
  onSpendTarget: (v: number) => void;
  onChange: (plan: Plan) => void;
  onRun: () => void;
  onThresholdPreview: (question: string, column: string, min: number) => void;
  onThresholdCommit: () => void;
  error: string | null;
  runLabel: string;
}

/** Source of truth for the workflow: editable operation steps, estimate and run control. */
export function OperationPanel(p: Props) {
  const { plan, desc } = p;
  if (!plan) return <div className="panel muted">Type a request above (for example “Find customers trying to cancel an order because they cannot afford it”) and press Plan. The planner sees the schema and at most 20 sample rows, never the table.</div>;
  const est = desc?.estimate;
  const updateStep = (i: number, step: Step) => { const steps = plan.steps.slice(); steps[i] = step; p.onChange({ ...plan, steps }); };
  return (
    <div className="panel" data-testid="operation-panel">
      {desc?.planner?.summary && <div className="banner info" style={{ borderRadius: 6 }}>{desc.planner.summary}</div>}
      {desc?.planner?.assumptions && desc.planner.assumptions.length > 0 && <ul className="assumptions muted">{desc.planner.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>}
      {plan.steps.map((step, i) => (
        <div className="step" key={step.id} data-testid={`step-${step.id}`}>
          <header><span className="op">{step.op}</span><span className="muted">{step.id} ← {step.input}</span></header>
          {step.op === "semantic_annotate" && (
            <>
              <div className="muted">columns: {step.columns.join(", ")} · missing → {step.on_missing ?? "unknown"}</div>
              {step.questions.map((q, qi) => (
                <QuestionEditor key={q.name} q={q} onChange={(nq) => { const qs = step.questions.slice(); qs[qi] = nq; updateStep(i, { ...step, questions: qs }); }}
                  onPreview={(min) => p.onThresholdPreview(q.name, `${q.name}.p`, min)} onCommit={p.onThresholdCommit} />
              ))}
            </>
          )}
          {step.op === "filter" && <div><span className="muted">where</span> <code>{exprText(step.where)}</code> · unknown rows: <select value={step.unknown_policy ?? "separate"} onChange={(e) => updateStep(i, { ...step, unknown_policy: e.target.value as any })}><option value="separate">review view</option><option value="exclude">exclude</option><option value="include">include</option></select></div>}
          {step.op === "sort" && (
            <div className="row">
              {step.by.map((k, ki) => (
                <span key={ki} className="row">
                  <code>{k.column}</code>
                  <select value={k.direction} onChange={(e) => { const by = step.by.slice(); by[ki] = { ...k, direction: e.target.value as any }; updateStep(i, { ...step, by }); }}><option value="desc">desc</option><option value="asc">asc</option></select>
                </span>
              ))}
              <span className="muted">score-based ranking; ties break on row id</span>
            </div>
          )}
          {step.op === "limit" && <div>top <input type="number" min={1} value={step.n} style={{ width: 90 }} onChange={(e) => updateStep(i, { ...step, n: Math.max(1, Number(e.target.value) || 1) })} /> rows</div>}
          {step.op === "project" && <div className="muted">keep: {step.columns.join(", ")}</div>}
          {step.op === "derive" && <div className="muted">{step.columns.map((c) => `${c.name} = ${exprText(c.expr)}`).join("; ")}</div>}
          {step.op === "aggregate" && <div className="muted">group by {step.group_by.join(", ") || "(all)"} → {step.metrics.map((m) => `${m.name}=${m.fn}(${m.column ?? "*"})`).join(", ")}</div>}
          {step.op === "join" && <div className="muted">{step.how ?? "left"} join {step.right.dataset_id} on {step.on.map((k) => `${k.left}=${k.right}`).join(", ")}</div>}
          {step.op === "dedupe" && <div className="muted">keys: {step.keys.join(", ")}</div>}
          {step.op === "semantic_match" && (
            <div>
              <div className="muted">right: {step.right.dataset_id} · {step.left_columns.join("+")} ↔ {step.right_columns.join("+")} · {step.candidates_per_row ?? 5} candidates/row</div>
              <textarea value={step.instruction} onChange={(e) => updateStep(i, { ...step, instruction: e.target.value })} />
              <div className="slider"><span>match ≥</span><input type="range" min={0.5} max={1} step={0.01} value={step.thresholds?.match_min ?? 0.85} onChange={(e) => updateStep(i, { ...step, thresholds: { match_min: Number(e.target.value), nonmatch_max: step.thresholds?.nonmatch_max ?? 0.15 } })} /><span>{(step.thresholds?.match_min ?? 0.85).toFixed(2)}</span></div>
            </div>
          )}
          {desc && <div className="muted" style={{ marginTop: 4 }}>{stepEstimate(desc, step.id)}</div>}
        </div>
      ))}
      <div className="muted">output: <code>{plan.output}</code></div>
      {est && (
        <div className="kv">
          <div className="k">estimate</div><div className="v">{est.semantic_rows.toLocaleString()} rows · {est.provider_requests.toLocaleString()} requests · {(est.input_tokens / 1e6).toFixed(2)}M tokens · <b>${est.cost_usd.toFixed(4)}</b> ({est.model})</div>
        </div>
      )}
      {desc?.warnings?.map((w, i) => <div key={i} className="banner" style={{ borderRadius: 6 }}>{w}</div>)}
      {p.error && <div className="error" data-testid="plan-error">{p.error}</div>}
      <div className="row">
        <label>spend target $<input type="number" step={0.05} min={0} value={p.spendTarget} style={{ width: 80 }} onChange={(e) => p.onSpendTarget(Number(e.target.value))} /></label>
        <button className="primary" disabled={p.busy} onClick={p.onRun} data-testid="plan-run">{p.busy ? "Working…" : p.runLabel}</button>
        {p.dirty && <span className="pill warn">edited · unchanged rows reuse stored predictions</span>}
      </div>
    </div>
  );
}

function QuestionEditor({ q, onChange, onPreview, onCommit }: { q: Question; onChange: (q: Question) => void; onPreview: (min: number) => void; onCommit: () => void }) {
  return (
    <div className="question" data-testid={`question-${q.name}`}>
      <div><b>{q.name}</b> <span className="pill">{q.kind}</span></div>
      <textarea value={q.instruction} onChange={(e) => onChange({ ...q, instruction: e.target.value } as Question)} />
      {q.kind === "boolean" && (
        <>
          <div className="slider"><span>true if p ≥</span>
            <input type="range" min={0.5} max={1} step={0.01} value={q.thresholds?.true_min ?? 0.85} data-testid={`slider-${q.name}-true`}
              onChange={(e) => { const v = Number(e.target.value); onChange({ ...q, thresholds: { true_min: v, false_max: Math.min(q.thresholds?.false_max ?? 0.15, v) } }); onPreview(v); }}
              onMouseUp={onCommit} onKeyUp={onCommit} onTouchEnd={onCommit} />
            <span>{(q.thresholds?.true_min ?? 0.85).toFixed(2)}</span></div>
          <div className="slider"><span>false if p ≤</span>
            <input type="range" min={0} max={0.5} step={0.01} value={q.thresholds?.false_max ?? 0.15}
              onChange={(e) => onChange({ ...q, thresholds: { true_min: q.thresholds?.true_min ?? 0.85, false_max: Number(e.target.value) } })} onMouseUp={onCommit} />
            <span>{(q.thresholds?.false_max ?? 0.15).toFixed(2)}</span></div>
        </>
      )}
      {q.kind === "category" && (
        <div>
          {q.labels.map((l, li) => (
            <div key={li} className="row" style={{ marginBottom: 3 }}>
              <input value={l.name} style={{ width: 150 }} onChange={(e) => { const labels = q.labels.slice(); labels[li] = { ...l, name: e.target.value }; onChange({ ...q, labels }); }} />
              <input value={l.description ?? ""} style={{ flex: 1 }} placeholder="description" onChange={(e) => { const labels = q.labels.slice(); labels[li] = { ...l, description: e.target.value }; onChange({ ...q, labels }); }} />
              <button onClick={() => onChange({ ...q, labels: q.labels.filter((_, j) => j !== li) })}>×</button>
            </div>
          ))}
          <button onClick={() => onChange({ ...q, labels: [...q.labels, { name: `label_${q.labels.length + 1}`, description: "" }] })}>+ label</button>
          <span className="muted"> + other, insufficient_evidence</span>
        </div>
      )}
      {q.kind === "score" && (
        <div>
          {q.levels.map((l, li) => (
            <div key={li} className="row" style={{ marginBottom: 3 }}><span className="muted">{li}</span><input value={l} style={{ flex: 1 }} onChange={(e) => { const levels = q.levels.slice(); levels[li] = e.target.value; onChange({ ...q, levels }); }} /></div>
          ))}
        </div>
      )}
    </div>
  );
}

function stepEstimate(desc: PlanDescription, id: string): string {
  const s = desc.steps.find((x) => x.id === id);
  if (!s) return "";
  const e = s.estimate as any;
  if (!s.semantic) return `${Object.keys(s.columns).length} columns`;
  if (e.pairs !== undefined) return `${e.pairs} candidate pairs · ${e.requests} requests · $${Number(e.cost_usd).toFixed(4)}`;
  return `${e.rows} rows × ${e.questions} question(s) · ${e.pack_rows} rows/request · ${e.requests} requests · $${Number(e.cost_usd).toFixed(4)}`;
}

export function exprText(e: any): string {
  if (!e || typeof e !== "object") return String(e);
  if ("operator" in e) return `${e.column} ${e.operator} ${JSON.stringify(e.value)}`;
  if (e.op === "and" || e.op === "or") return "(" + e.args.map(exprText).join(` ${e.op} `) + ")";
  if (e.op === "not") return `not ${exprText(e.arg)}`;
  if ("op" in e) return `${exprText(e.left)} ${e.op} ${e.right && typeof e.right === "object" && !("value" in e.right) ? exprText(e.right) : JSON.stringify(e.right?.value ?? e.right)}`;
  if ("fn" in e) return `${e.fn}(${(e.args || []).map(exprText).join(", ")})`;
  if ("column" in e) return e.column;
  if ("value" in e) return JSON.stringify(e.value);
  return JSON.stringify(e);
}
