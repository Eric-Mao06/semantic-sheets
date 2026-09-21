import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, subscribeJobEvents } from "../api";
import { ViewController, type ViewSpec } from "../data/ViewController";
import type { ColumnInfo, DatasetInfo, Job, JobEvent, Plan, Question, ResultDescribe, Row, ValidateResponse, WorkspaceInfo } from "../types";
import Grid from "./Grid";
import Inspector from "./Inspector";
import PlanPanel, { type Limits } from "./PlanPanel";
import QuickFilter, { type LocalFilterResult } from "./QuickFilter";
import Versions from "./Versions";
import type { Preview } from "./Landing";

type Props = { dataset: DatasetInfo; workspace: WorkspaceInfo; initialPrompt?: string; note?: string; preview?: Preview | null; onBack: () => void; onWorkspaceRefresh: () => void };

type SideTab = "plan" | "inspect" | "versions";

export default function Workbench({ dataset, workspace, initialPrompt, note, preview, onBack, onWorkspaceRefresh }: Props) {
  const [activeRv, setActiveRv] = useState<string | null>(null);
  const [result, setResult] = useState<ResultDescribe | null>(null);
  const [step, setStep] = useState<string>("source");
  const userPickedStep = useRef(false);
  const [columns, setColumns] = useState<ColumnInfo[]>(dataset.schema.map((c) => ({ name: c.name, type: c.type, role: c.role })));
  const controller = useMemo(() => new ViewController({ rv: "", step: "source", columns: [], revision: 0 }), []);
  const [dataVersion, setDataVersion] = useState(0);
  const [prompt, setPrompt] = useState(initialPrompt ?? "");
  const [compiling, setCompiling] = useState(false);
  const [compileError, setCompileError] = useState<string | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [validation, setValidation] = useState<ValidateResponse | null>(null);
  const [validating, setValidating] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [limits, setLimits] = useState<Limits>({ max_source_rows: Math.min(dataset.row_count, workspace.limits.default_max_source_rows), max_provider_requests: 12000, spend_target_usd: 0.5, deadline_seconds: 600, rows_per_request: 10 });
  const [job, setJob] = useState<Job | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [sideTab, setSideTab] = useState<SideTab>("plan");
  const [selected, setSelected] = useState<{ row: Row | null; column: ColumnInfo | null }>({ row: null, column: null });
  const [pendingEdits, setPendingEdits] = useState<Map<string, unknown>>(new Map());
  const [updatesAvailable, setUpdatesAvailable] = useState(false);
  const [toast, setToast] = useState<string | null>(note ?? null);
  const [exportInfo, setExportInfo] = useState<Parameters<typeof Versions>[0]["exportInfo"]>(null);
  const [localFilter, setLocalFilter] = useState<LocalFilterResult | null>(null);
  const [wsInfo, setWsInfo] = useState<WorkspaceInfo>(workspace);
  const unsubscribe = useRef<(() => void) | null>(null);
  const activeRvRef = useRef<string | null>(null);
  useEffect(() => {
    activeRvRef.current = activeRv;
  }, [activeRv]);

  const showToast = useCallback((m: string) => {
    setToast(m);
    window.setTimeout(() => setToast((t) => (t === m ? null : t)), 4500);
  }, []);

  useEffect(() => controller.subscribe(() => setDataVersion((v) => v + 1)), [controller]);

  // Base view: identity result over the dataset version.
  useEffect(() => {
    let cancelled = false;
    api.baseQuery(dataset.dataset_id, { version_id: dataset.version_id, limit: 1 }).then((r) => {
      if (cancelled) return;
      setActiveRv(r.result_version_id);
      api.jobs(dataset.version_id).then((j) => setJobs(j.jobs)).catch(() => {});
    });
    return () => {
      cancelled = true;
    };
  }, [dataset.dataset_id, dataset.version_id]);

  // Load result description when the active result version changes.
  useEffect(() => {
    if (!activeRv) return;
    let cancelled = false;
    api.describeResult(activeRv).then((r) => {
      if (cancelled) return;
      setResult(r);
      const isBase = r.job_id === null;
      const semantic = r.steps.find((s) => s.op === "semantic_annotate" || s.op === "semantic_match");
      const nextStep = isBase ? "source" : userPickedStep.current && r.steps.some((s) => s.id === step) ? step : r.status === "running" && semantic ? semantic.id : r.output;
      setStep(nextStep);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRv]);

  // Columns + view spec follow (activeRv, step, localFilter).
  useEffect(() => {
    if (!activeRv || !result) return;
    let cols: ColumnInfo[];
    if (step === "source") cols = dataset.schema.map((c) => ({ name: c.name, type: c.type, role: c.role }));
    else {
      const s = result.steps.find((x) => x.id === step) ?? result.steps.find((x) => x.review_view === step);
      cols = step.endsWith("__review") ? (result.steps.find((x) => x.review_view === step) ? result.steps.find((x) => x.id === step.replace(/__review$/, ""))!.columns.slice() : []) : s?.columns ?? [];
      if (step.endsWith("__review")) {
        const src = result.steps.find((x) => x.id === step.replace(/__review$/, ""));
        const inputStep = src ? result.steps.find((x) => x.id === src.input) : null;
        cols = inputStep?.columns ?? cols;
      }
    }
    const stepDef = result.steps.find((x) => x.id === step);
    if (stepDef && (stepDef.op === "semantic_annotate" || stepDef.op === "semantic_match")) {
      const isOut = (c: ColumnInfo) => c.name.includes(".") && !c.name.endsWith(".raw");
      cols = [...cols.filter((c) => c.name === "_row_id"), ...cols.filter((c) => c.name !== "_row_id" && isOut(c)), ...cols.filter((c) => c.name !== "_row_id" && !isOut(c))];
    }
    setColumns(cols);
    const spec: ViewSpec = { rv: activeRv, step, columns: cols.map((c) => c.name), revision: result.revision, localRowIds: localFilter?.rowIds ?? null };
    controller.setSpec(spec);
    setPendingEdits(new Map());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRv, result, step, localFilter?.rowIds]);

  // ---- planning -------------------------------------------------------------------------------
  const compile = async () => {
    if (!prompt.trim()) return;
    setCompiling(true);
    setCompileError(null);
    setSideTab("plan");
    try {
      const r = await api.compile(dataset.dataset_id, dataset.version_id, prompt, plan);
      setPlan(r.plan);
      setValidation(r);
      setValidationError(null);
      showToast(`Planned with ${r.planner?.model} in ${((r.planner?.latency_ms ?? 0) / 1000).toFixed(1)}s`);
    } catch (e) {
      setCompileError((e as Error).message);
    } finally {
      setCompiling(false);
    }
  };

  const validateTimer = useRef<number | null>(null);
  const onPlanChange = (p: Plan) => {
    setPlan(p);
    setValidating(true);
    if (validateTimer.current) window.clearTimeout(validateTimer.current);
    validateTimer.current = window.setTimeout(async () => {
      try {
        const v = await api.validate(p, limits);
        setValidation(v);
        setValidationError(null);
      } catch (e) {
        const err = e as { message: string; details?: { issues?: { path: string; message: string; fix?: string }[] } };
        const issue = err.details?.issues?.[0];
        setValidationError(issue ? `${issue.path}: ${issue.message}${issue.fix ? ` — ${issue.fix}` : ""}` : err.message);
      } finally {
        setValidating(false);
      }
    }, 450);
  };

  // ---- jobs -------------------------------------------------------------------------------------
  const attachJob = useCallback(
    (j: Job) => {
      setJob(j);
      unsubscribe.current?.();
      unsubscribe.current = subscribeJobEvents(
        j.job_id,
        (ev: JobEvent) => {
          if (ev.type === "chunk_committed") {
            const range = ev.row_range as [number, number];
            setJob((old) => (old ? { ...old, state: "running", progress: ev.progress as Job["progress"], usage: ev.usage as Job["usage"], result_revision: ev.revision as number } : old));
            if (controller.spec.rv === j.result_version_id) {
              // Refresh visible cells only when we are looking at a row-preserving step in source order.
              if (!controller.spec.localRowIds) controller.invalidateRowRange(range[0], range[1], ev.revision as number);
              else setUpdatesAvailable(true);
            }
          } else if (ev.type === "finished") {
            api.job(j.job_id).then((jj) => {
              setJob(jj);
              setJobs((list) => [jj, ...list.filter((x) => x.job_id !== jj.job_id)]);
              api.workspace().then(setWsInfo).catch(() => {});
              onWorkspaceRefresh();
              if (activeRvRef.current === jj.result_version_id) {
                api.describeResult(jj.result_version_id).then((r) => {
                  setResult(r);
                  if (!userPickedStep.current) setStep(r.output);
                  else setUpdatesAvailable(true);
                });
              }
              showToast(`Job ${jj.state}${jj.terminal_reason ? ` (${jj.terminal_reason})` : ""} · $${jj.usage.spent_usd.toFixed(4)} · ${jj.usage.provider_requests} requests · ${jj.usage.cache_hits} cache hits`);
            });
          }
        },
        () => {},
      );
    },
    [controller, onWorkspaceRefresh, showToast],
  );

  const run = async () => {
    if (!validation) return;
    try {
      const j = await api.submit(validation.plan_hash, limits, crypto.randomUUID());
      userPickedStep.current = false;
      setLocalFilter(null);
      setUpdatesAvailable(false);
      setActiveRv(j.result_version_id);
      setJobs((list) => [j, ...list.filter((x) => x.job_id !== j.job_id)]);
      attachJob(j);
    } catch (e) {
      showToast(`Could not start job: ${(e as Error).message}`);
    }
  };

  const cancel = async () => {
    if (!job) return;
    setJob(await api.cancel(job.job_id));
  };

  useEffect(() => () => unsubscribe.current?.(), []);

  // ---- corrections -----------------------------------------------------------------------------
  const correct = async (rowId: number, column: string, value: unknown, reason: string) => {
    if (!activeRv) return;
    setPendingEdits((m) => new Map(m).set(`${rowId}:${column}`, value));
    try {
      const r = await api.patch(activeRv, [{ row_id: rowId, column, value, reason: reason || undefined }]);
      userPickedStep.current = true;
      setActiveRv(r.result_version_id);
      showToast("Correction saved as a new result version (the model output is preserved).");
    } catch (e) {
      setPendingEdits((m) => {
        const n = new Map(m);
        n.delete(`${rowId}:${column}`);
        return n;
      });
      showToast(`Correction rejected: ${(e as Error).message}`);
    }
  };

  const semanticStepForColumn = (col: ColumnInfo | null): { stepId: string | null; questions: Question[] } => {
    if (!col || !result || !col.name.includes(".")) return { stepId: null, questions: [] };
    const q = col.name.split(".")[0];
    for (const s of result.plan.steps) {
      if (s.op === "semantic_annotate" && s.questions.some((x) => x.name === q)) return { stepId: s.id, questions: s.questions };
    }
    return { stepId: null, questions: [] };
  };

  const editable = useMemo(() => {
    const set = new Set<string>();
    if (!result || step === "source" || step.endsWith("__review")) return set;
    for (const s of result.plan.steps) if (s.op === "semantic_annotate") for (const q of s.questions) set.add(`${q.name}.value`);
    return set;
  }, [result, step]);

  const doExport = async (format: "csv" | "parquet", raw: boolean) => {
    if (!activeRv) return;
    try {
      setExportInfo(await api.export(activeRv, format, step === "source" ? undefined : step.endsWith("__review") ? step : step, raw));
    } catch (e) {
      showToast(`Export failed: ${(e as Error).message}`);
    }
  };

  // ---- derived ---------------------------------------------------------------------------------
  const meta = controller.meta;
  const stageEntries = job ? Object.entries(job.progress.stages) : [];
  const examined = job?.progress.rows_examined ?? 0;
  const remaining = job?.progress.rows_remaining ?? 0;
  const totalRows = stageEntries.reduce((a, [, s]) => a + s.rows_total, 0);
  const stepInfo = result?.steps.find((s) => s.id === step);
  const semanticStep = result?.steps.find((s) => s.op === "semantic_annotate" || s.op === "semantic_match");
  const isRunning = job && (job.state === "queued" || job.state === "running");
  const budgetLeft = Math.max(0, wsInfo.budget_usd - wsInfo.spent_usd);

  return (
    <div className="workbench">
      <div className="topbar">
        <button className="btn small" onClick={onBack}>← Datasets</button>
        <div>
          <div className="title">{dataset.name}</div>
          <div className="sub">
            {dataset.row_count.toLocaleString()} rows · {dataset.column_count} columns · v{dataset.version_no}
            {dataset.import_report?.rejected_rows ? ` · ${dataset.import_report.rejected_rows} rejected rows` : ""}
            {preview ? " · provisional preview" : ""}
          </div>
        </div>
        <span className="grow" />
        <div className="budget">
          workspace spend <b>${wsInfo.spent_usd.toFixed(4)}</b> of ${wsInfo.budget_usd.toFixed(2)} · Jev {wsInfo.model} · planner {wsInfo.planner_model}
        </div>
        <a className="btn small" href="/mcp" onClick={(e) => { e.preventDefault(); showToast("MCP endpoint: POST /mcp (Streamable HTTP) with the same bearer token."); }}>MCP</a>
      </div>

      <div className="commandbar">
        <input
          type="text"
          placeholder="Describe an operation… e.g. “Find customers trying to cancel an order because they cannot afford it, and rate how urgent each message is”"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !compiling && compile()}
          disabled={compiling}
        />
        <button className="btn primary" onClick={compile} disabled={compiling || !prompt.trim()}>
          {compiling ? <span className="row"><span className="spinner" /> Planning…</span> : plan ? "Refine plan" : "Plan"}
        </button>
      </div>

      <div className="main">
        <div className="gridarea">
          <div className="steptabs">
            <span className={"tab" + (step === "source" ? " active" : "")} onClick={() => { userPickedStep.current = true; setLocalFilter(null); setStep("source"); }}>source</span>
            {result?.job_id &&
              result.steps.map((s) => (
                <span key={s.id} className={"tab" + (step === s.id ? " active" : "")} onClick={() => { userPickedStep.current = true; setLocalFilter(null); setUpdatesAvailable(false); setStep(s.id); }}>
                  {s.id}
                  <span className="op">{s.op}</span>
                  {s.provisional && <span className="badge pending" style={{ marginLeft: 4 }}>provisional</span>}
                  {s.id === result.output && <span className="badge ok" style={{ marginLeft: 4 }}>output</span>}
                </span>
              ))}
            {result?.job_id &&
              result.steps.filter((s) => s.review_view).map((s) => (
                <span key={s.review_view} className={"tab" + (step === s.review_view ? " active" : "")} onClick={() => { userPickedStep.current = true; setLocalFilter(null); setStep(s.review_view!); }} title="Rows with uncertain, missing, failed or pending answers for this filter">
                  {s.id} · review
                </span>
              ))}
            <span className="grow" />
            {meta.total !== null && (
              <span className="muted" style={{ whiteSpace: "nowrap" }}>
                <b>{meta.total.toLocaleString()}</b> rows{meta.denominator !== null && meta.denominator !== meta.total ? ` of ${meta.denominator.toLocaleString()} scanned` : ""}
                {meta.countStatus === "partial" ? " · provisional" : ""}
                {localFilter ? ` · local filter ${localFilter.ms.toFixed(0)} ms` : ""}
              </span>
            )}
          </div>
          {result?.job_id && stepInfo && stepInfo.row_preserving && (
            <QuickFilter rv={activeRv!} step={step} columns={columns} revision={result.revision} enabled={!step.endsWith("__review")} onResult={setLocalFilter} />
          )}
          <div className="gridwrap">
            {updatesAvailable && (
              <button className="btn primary small updates" onClick={() => { setUpdatesAvailable(false); userPickedStep.current = false; if (result) setStep(result.output); setLocalFilter(null); }}>
                Updates available · show result
              </button>
            )}
            {activeRv && columns.length > 0 ? (
              <Grid controller={controller} columns={columns} dataVersion={dataVersion} editable={editable} pendingEdits={pendingEdits} onCellClick={(idx, col) => { setSelected({ row: controller.getRow(idx) ?? null, column: col }); setSideTab("inspect"); }} onEdit={(rowId, column, value) => void correct(rowId, column, value, "inline edit")} />
            ) : (
              <div className="overlay"><span className="spinner" />&nbsp; loading view…</div>
            )}
            {meta.error && <div className="overlay"><div className="error">{meta.error}</div></div>}
          </div>
        </div>

        <div className="side">
          <div className="tabs">
            <span className={"tab" + (sideTab === "plan" ? " active" : "")} onClick={() => setSideTab("plan")}>Operation</span>
            <span className={"tab" + (sideTab === "inspect" ? " active" : "")} onClick={() => setSideTab("inspect")}>Inspect</span>
            <span className={"tab" + (sideTab === "versions" ? " active" : "")} onClick={() => setSideTab("versions")}>Versions & export</span>
          </div>
          {sideTab === "plan" && (
            <>
              {compileError && <div className="error" style={{ margin: 12 }}>{compileError}</div>}
              <PlanPanel plan={plan} validation={validation} validating={validating} validationError={validationError} schema={dataset.schema} limits={limits} job={job} budgetLeftUsd={budgetLeft} onPlanChange={onPlanChange} onLimitsChange={(l) => { setLimits(l); if (plan) onPlanChange(plan); }} onRun={run} onCancel={cancel} onClear={() => { setPlan(null); setValidation(null); }} />
            </>
          )}
          {sideTab === "inspect" && activeRv && (
            <Inspector rv={activeRv} step={step} row={selected.row} column={selected.column} semanticStep={semanticStepForColumn(selected.column).stepId} questions={semanticStepForColumn(selected.column).questions} onCorrect={correct} />
          )}
          {sideTab === "versions" && (
            <Versions rv={result?.job_id ? activeRv : null} activeRv={activeRv} jobs={jobs} onSelect={(rv) => { userPickedStep.current = false; setLocalFilter(null); setActiveRv(rv); setJob(jobs.find((j) => j.result_version_id === rv) ?? null); }} onExport={doExport} exportInfo={exportInfo} />
          )}
        </div>
      </div>

      <div className="statusstrip">
        {job ? (
          <>
            <span className={"badge " + (job.state === "running" ? "accent" : job.state === "succeeded" ? "ok" : job.state === "partial" ? "warn" : job.state === "failed" ? "bad" : "")}>{job.state}{job.terminal_reason ? ` · ${job.terminal_reason}` : ""}</span>
            <div className="progress" title={`${examined} of ${totalRows} rows`}><div style={{ width: `${totalRows ? Math.min(100, (100 * examined) / totalRows) : 0}%` }} /></div>
            <span><b>{examined.toLocaleString()}</b> examined</span>
            <span><b>{remaining.toLocaleString()}</b> remaining</span>
            <span><b>{job.progress.errors}</b> errors</span>
            {stageEntries.map(([id, s]) => (
              <span key={id} title="succeeded / uncertain / missing input">{id}: {s.rows_succeeded}✓ {s.rows_uncertain}? {s.rows_missing}∅{s.rows_beyond_cap ? ` · ${s.rows_beyond_cap} beyond cap` : ""}</span>
            ))}
            <span>spend <b>${job.usage.spent_usd.toFixed(4)}</b> · {job.usage.provider_requests} requests · {job.usage.cache_hits} cache hits · {(job.usage.input_tokens / 1000).toFixed(1)}k tokens</span>
            {isRunning && <span className="spinner" />}
          </>
        ) : (
          <span>{semanticStep ? "" : "No job running."} {meta.loading ? "loading…" : ""} cache {controller.cacheStats().blocks} blocks · {(controller.cacheStats().bytes / 1024).toFixed(0)} KB</span>
        )}
        <span className="grow" />
        {dataset.retention_expires_at && <span>retained until {new Date(dataset.retention_expires_at * 1000).toLocaleDateString()}</span>}
      </div>
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
