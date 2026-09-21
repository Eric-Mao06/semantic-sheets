import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DataEditorRef } from "@glideapps/glide-data-grid";
import { api, subscribeJob } from "../api";
import { BlockCache } from "../data/BlockCache";
import type { ColumnDef, DatasetDescription, JobDescription, Plan, PlanDescription, ResultColumns, ResultVersionInfo, WorkspaceInfo } from "../types";
import { CommandBar } from "./CommandBar";
import { Grid } from "./Grid";
import { Inspector } from "./Inspector";
import { OperationPanel } from "./OperationPanel";
import { StatusStrip } from "./StatusStrip";

type View = { kind: "dataset"; versionId: string | null } | { kind: "result"; rv: string; reviewOf: string | null };

const SUGGESTIONS = [
  "Find customers trying to cancel an order because they cannot afford it",
  "Classify each message by issue type and rate its urgency, then show the most urgent ones first",
  "Find complaints about charges continuing after cancellation, then rank by urgency",
  "Separate pending transfers, failed transfers, and transfers sent to the wrong account",
  "Find reviews reporting unreliable Wi-Fi",
];

export function Sheet({ datasetId, initialRv, workspace, onWorkspaceChange }: { datasetId: string; initialRv: string | null; workspace: WorkspaceInfo; onWorkspaceChange: () => void }) {
  const [dataset, setDataset] = useState<DatasetDescription | null>(null);
  const [view, setView] = useState<View>(initialRv ? { kind: "result", rv: initialRv, reviewOf: null } : { kind: "dataset", versionId: null });
  const [resultCols, setResultCols] = useState<ResultColumns | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [planDesc, setPlanDesc] = useState<PlanDescription | null>(null);
  const [planDirty, setPlanDirty] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<JobDescription | null>(null);
  const [versions, setVersions] = useState<ResultVersionInfo[]>([]);
  const [jobs, setJobs] = useState<JobDescription[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [tab, setTab] = useState<"plan" | "inspect" | "versions">("plan");
  const [spendTarget, setSpendTarget] = useState(0.5);
  const [notice, setNotice] = useState<string | null>(null);
  const [localCount, setLocalCount] = useState<{ count: number; ms: number; label: string } | null>(null);
  const [exportInfo, setExportInfo] = useState<{ url: string; rows: number; complete: boolean; format: string } | null>(null);
  const gridRef = useRef<DataEditorRef>(null);
  const workerRef = useRef<Worker | null>(null);
  const vectorsLoadedFor = useRef<string | null>(null);
  const unsubRef = useRef<() => void>();
  const pendingRevision = useRef<number | null>(null);

  // ---- dataset ------------------------------------------------------------------------------------------
  useEffect(() => {
    api.dataset(datasetId).then(setDataset).catch((e) => setNotice(e.message));
    api.datasetJobs(datasetId).then((j) => setJobs(j.jobs)).catch(() => undefined);
  }, [datasetId]);

  // ---- columns for the current view ---------------------------------------------------------------------
  useEffect(() => {
    if (view.kind !== "result") { setResultCols(null); return; }
    api.resultColumns(view.rv).then((rc) => {
      setResultCols(rc);
      api.versions(view.rv).then((v) => setVersions(v.versions)).catch(() => undefined);
      if (!job || job.job_id !== rc.job_id) api.job(rc.job_id).then((j) => { setJob(j); if (j.state === "queued" || j.state === "running") follow(j); }).catch(() => undefined);
      if (!plan) api.job(rc.job_id).then((j) => fetch(`/api/v1/plans/${j.plan_version_id}`, { headers: { Authorization: `Bearer ${localStorage.getItem("ss.workspace_key") || import.meta.env.VITE_WORKSPACE_KEY || ""}` } }).then((r) => r.json()).then((d: PlanDescription) => { setPlan(d.plan); setPlanDesc(d); })).catch(() => undefined);
    }).catch((e) => setNotice(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  const columns: ColumnDef[] = useMemo(() => {
    if (view.kind === "dataset") return dataset ? [{ name: "_row_id", type: "integer" }, ...dataset.schema.map((c) => ({ name: c.name, type: c.type }))] : [];
    if (!resultCols) return [];
    if (view.reviewOf) { const st = resultCols.steps.find((s) => s.id === view.reviewOf); return st ? st.columns.map((n) => ({ name: n, type: resultCols.columns.find((c) => c.name === n)?.type ?? "text" })) : resultCols.columns; }
    return resultCols.columns;
  }, [view, dataset, resultCols]);

  // ---- block cache bound to the view ---------------------------------------------------------------------
  const cache = useMemo(() => {
    const names = columns.map((c) => c.name);
    if (view.kind === "dataset") {
      const c = new BlockCache(async (start, limit, signal) => {
        const r = await api.datasetRows(datasetId, start, limit, undefined, view.versionId, signal);
        return { rows: r.rows, total: r.total_count, countStatus: r.count_status, provisional: false, truncated: r.truncated_cells };
      });
      c.columns = columns; return c;
    }
    const rv = view.rv; const reviewOf = view.reviewOf;
    const c = new BlockCache(async (start, limit, signal) => {
      const spec: Record<string, unknown> = { start, limit, columns: names.length ? names : undefined };
      if (reviewOf) spec.scope = { review_of: reviewOf };
      const r = await api.query(rv, spec, signal);
      return { rows: r.rows, total: r.total_count ?? null, countStatus: r.count_status, provisional: r.provisional, truncated: r.truncated_cells };
    });
    c.columns = columns; return c;
  }, [view, columns, datasetId]);
  const cacheRef = useRef(cache);
  cacheRef.current = cache;
  const [, bump] = useState(0);
  useEffect(() => { cache.ensure(0, 64); return cache.subscribe(() => bump((n) => n + 1)); }, [cache]);

  // ---- job following ------------------------------------------------------------------------------------
  const follow = useCallback((j: JobDescription) => {
    unsubRef.current?.();
    let coalesce: number | null = null;
    unsubRef.current = subscribeJob(j.job_id, 0, (ev) => {
      if (ev.kind === "progress" || ev.kind === "finished") {
        setJob((cur) => cur ? { ...cur, progress: (ev.progress as any) ?? cur.progress, usage: (ev.usage as any) ?? cur.usage, result_revision: (ev.result_revision as number) ?? cur.result_revision, state: ev.kind === "finished" ? (ev.state as any) : "running", terminal_reason: ev.kind === "finished" ? ((ev.reason as string) ?? null) : cur.terminal_reason } : cur);
        pendingRevision.current = (ev.result_revision as number) ?? pendingRevision.current;
        if (coalesce === null) coalesce = window.setTimeout(() => { coalesce = null; const c = cacheRef.current; c.invalidate(true); c.ensure(0, 64); }, 100);
      }
      if (ev.kind === "finished") {
        window.setTimeout(() => { api.job(j.job_id).then((jj) => { setJob(jj); onWorkspaceChange(); api.datasetJobs(datasetId).then((x) => setJobs(x.jobs)).catch(() => undefined); api.versions(jj.result_version_id).then((v) => setVersions(v.versions)).catch(() => undefined); const c = cacheRef.current; c.invalidate(true); c.ensure(0, 64); }).catch(() => undefined); }, 250);
      }
    });
  }, [datasetId, onWorkspaceChange]);
  useEffect(() => () => unsubRef.current?.(), []);

  // ---- planning & running ---------------------------------------------------------------------------------
  const compile = async (request: string) => {
    setBusy(true); setPlanError(null); setNotice("Planning with " + workspace.planner_model + " (bounded schema + 20 sample rows)…"); setTab("plan");
    try {
      const d = await api.compile(datasetId, request, plan);
      setPlan(d.plan); setPlanDesc(d); setPlanDirty(false); setNotice(null);
    } catch (e: any) { setPlanError(e.message + (e.path ? ` (at ${e.path})` : "")); setNotice(null); }
    finally { setBusy(false); }
  };

  const run = async () => {
    if (!plan) return;
    setBusy(true); setPlanError(null); setLocalCount(null);
    try {
      const d = await api.validate(plan);
      setPlanDesc((cur) => ({ ...d, planner: cur?.planner }));
      const j = await api.submit({ plan_hash: d.plan_hash, limits: { spend_target_usd: spendTarget }, idempotency_key: `sheet:${d.plan_hash}:${Date.now()}` });
      setJob(j); setPlanDirty(false);
      setView({ kind: "result", rv: j.result_version_id, reviewOf: null });
      location.hash = `/d/${datasetId}?rv=${j.result_version_id}`;
      follow(j);
    } catch (e: any) { setPlanError(e.message + (e.path ? ` (at ${e.path})` : "")); }
    finally { setBusy(false); }
  };

  const cancel = async () => { if (job) { const j = await api.cancel(job.job_id); setJob(j); } };

  // ---- local threshold preview over complete vectors -------------------------------------------------------
  const ensureVectors = useCallback(async (rv: string, column: string) => {
    if (vectorsLoadedFor.current === `${rv}:${column}`) return true;
    if (!workerRef.current) workerRef.current = new Worker(new URL("../data/filter.worker.ts", import.meta.url), { type: "module" });
    const v = await api.vectors(rv, [column]);
    const rowIds = new Int32Array(v.row_ids);
    const vals = v.columns[column].values;
    const arr = new Float64Array(vals.length);
    for (let i = 0; i < vals.length; i++) arr[i] = vals[i] == null ? NaN : Number(vals[i]);
    workerRef.current.postMessage({ type: "load", rowIds, numeric: { [column]: arr }, labels: {} }, [rowIds.buffer, arr.buffer]);
    vectorsLoadedFor.current = `${rv}:${column}`;
    return true;
  }, []);
  const previewSeq = useRef(0);
  const thresholdPreview = useCallback(async (question: string, column: string, min: number) => {
    if (view.kind !== "result" || !resultCols) return;
    const stepId = resultCols.steps.find((s) => s.semantic)?.id;
    const srcRv = resultCols.result_version_id;
    try {
      await ensureVectors(srcRv, column);
      const id = ++previewSeq.current;
      const w = workerRef.current!;
      const handler = (e: MessageEvent) => { if (e.data.type === "result" && e.data.id === id) { w.removeEventListener("message", handler); setLocalCount({ count: e.data.count, ms: e.data.ms, label: `${question} ≥ ${min.toFixed(2)}` }); } };
      w.addEventListener("message", handler);
      w.postMessage({ type: "threshold", id, column, min, max: null, limit: 256 });
    } catch { /* vectors unavailable for this scope */ }
    void stepId;
  }, [view, resultCols, ensureVectors]);

  // ---- corrections ----------------------------------------------------------------------------------------
  const correct = useCallback(async (rowId: number, column: string, value: unknown) => {
    try {
      if (view.kind === "result") {
        const r = await api.resultPatch(view.rv, [{ row_id: rowId, column, value }]);
        setView({ kind: "result", rv: r.result_version_id, reviewOf: view.reviewOf });
        location.hash = `/d/${datasetId}?rv=${r.result_version_id}`;
        setNotice(`Correction saved as result version ${r.number}. Earlier versions remain selectable under Versions.`);
      } else {
        const r = await api.datasetPatch(datasetId, [{ row_id: rowId, column, value }], view.versionId);
        setView({ kind: "dataset", versionId: r.version_id });
        api.dataset(datasetId).then(setDataset);
        setNotice(`Source correction saved as dataset version ${r.number}; dependent predictions are recomputed on the next run.`);
      }
    } catch (e: any) { setNotice("Correction rejected: " + e.message); }
  }, [view, datasetId]);

  const doExport = async (format: "csv" | "parquet") => {
    if (view.kind !== "result") return;
    try {
      const e = await api.export({ result_version_id: view.rv, format, scope: view.reviewOf ? { review_of: view.reviewOf } : undefined });
      setExportInfo({ url: api.downloadUrl(e.export_id), rows: e.row_count, complete: Boolean((e.manifest as any).complete), format });
    } catch (err: any) { setNotice("Export failed: " + err.message); }
  };

  const total = cache.total ?? (view.kind === "dataset" ? dataset?.row_count ?? null : null);
  const rows = total ?? 0;
  const filterSteps = resultCols?.steps.filter((s) => s.op === "filter") ?? [];
  const viewLabel = view.kind === "dataset" ? `source${view.versionId ? " (" + view.versionId.slice(0, 8) + ")" : ""}` : view.reviewOf ? `review: unknown rows of ${view.reviewOf}` : `result ${resultCols ? "v" + (versions.find((v) => v.result_version_id === view.rv)?.number ?? "") : ""}`;

  return (
    <div className="sheet">
      <div className="topbar">
        <a href="#/">← datasets</a>
        <h1 data-testid="dataset-name">{dataset?.name ?? datasetId}</h1>
        <span className="muted">{dataset ? `${dataset.row_count.toLocaleString()} rows · ${dataset.schema.length} columns · ${dataset.versions.length} version(s)` : ""}</span>
        <span style={{ marginLeft: "auto" }} className="row">
          <button onClick={() => { setView({ kind: "dataset", versionId: null }); location.hash = `/d/${datasetId}`; }} disabled={view.kind === "dataset"} data-testid="view-source">Source</button>
          {job && <button onClick={() => { setView({ kind: "result", rv: job.result_version_id, reviewOf: null }); location.hash = `/d/${datasetId}?rv=${job.result_version_id}`; }} disabled={view.kind === "result" && view.rv === job.result_version_id && !view.reviewOf} data-testid="view-result">Result</button>}
          {view.kind === "result" && filterSteps.map((f) => (
            <button key={f.id} onClick={() => setView({ kind: "result", rv: view.rv, reviewOf: view.reviewOf === f.id ? null : f.id })} data-testid={`review-${f.id}`}>{view.reviewOf === f.id ? "Hide" : "Review"} unknown rows of {f.id}</button>
          ))}
          {view.kind === "result" && <><button onClick={() => doExport("csv")} data-testid="export-csv">Export CSV</button><button onClick={() => doExport("parquet")} data-testid="export-parquet">Export Parquet</button></>}
        </span>
      </div>
      <CommandBar busy={busy} onCompile={compile} placeholder={`Ask for an operation over ${dataset?.name ?? "this table"}…`} suggestions={SUGGESTIONS} />
      <div className="gridwrap">
        {notice && <div className="banner info" data-testid="notice">{notice} <button style={{ marginLeft: 8 }} onClick={() => setNotice(null)}>×</button></div>}
        {exportInfo && <div className="banner" data-testid="export-banner">Export ready: <a href={exportInfo.url} download>{exportInfo.format} · {exportInfo.rows.toLocaleString()} rows</a> · manifest marks it {exportInfo.complete ? "complete" : "PARTIAL"} <button style={{ marginLeft: 8 }} onClick={() => setExportInfo(null)}>×</button></div>}
        {cache.provisional && <div className="banner">Provisional: rankings and counts update while the job runs ({job?.progress?.pending ?? "?"} rows remaining).</div>}
        <div style={{ position: "absolute", inset: 0, top: (notice ? 30 : 0) + (exportInfo ? 30 : 0) + (cache.provisional ? 30 : 0) }}>
          {columns.length > 0 && <Grid ref={gridRef} columns={columns} cache={cache} rows={rows} editable
            onActivate={(_o, rid) => { setSelected(rid); setTab("inspect"); }}
            onEdit={(rid, col, val) => correct(rid, col, val)} />}
        </div>
      </div>
      <div className="side">
        <div className="tabs">
          <button className={tab === "plan" ? "active" : ""} onClick={() => setTab("plan")} data-testid="tab-plan">Operation</button>
          <button className={tab === "inspect" ? "active" : ""} onClick={() => setTab("inspect")} data-testid="tab-inspect">Inspector</button>
          <button className={tab === "versions" ? "active" : ""} onClick={() => setTab("versions")} data-testid="tab-versions">Versions</button>
        </div>
        {tab === "plan" && <OperationPanel plan={plan} desc={planDesc} dirty={planDirty} busy={busy} spendTarget={spendTarget} onSpendTarget={setSpendTarget}
          onChange={(p) => { setPlan(p); setPlanDirty(true); }} onRun={run} error={planError}
          onThresholdPreview={thresholdPreview} onThresholdCommit={() => undefined}
          runLabel={job && !planDirty ? "Run again" : planDirty ? "Apply changes (reuses predictions)" : "Run"} />}
        {tab === "inspect" && <Inspector rv={view.kind === "result" ? view.rv : null} rowId={selected} onCorrect={correct}
          onDatasetCell={async (rid) => { const r = await api.datasetRows(datasetId, rid, 1, undefined, view.kind === "dataset" ? view.versionId : null); const o: Record<string, unknown> = {}; r.columns.forEach((c, i) => { o[c] = r.rows[0]?.[i]; }); return o; }} />}
        {tab === "versions" && (
          <div className="panel versions" data-testid="versions-panel">
            <div><b>Result versions</b> <span className="muted">(undo = select an earlier version)</span></div>
            {versions.map((v) => (
              <button key={v.result_version_id} className={view.kind === "result" && view.rv === v.result_version_id ? "active" : ""}
                onClick={() => { setView({ kind: "result", rv: v.result_version_id, reviewOf: null }); location.hash = `/d/${datasetId}?rv=${v.result_version_id}`; }}>
                v{v.number} · {v.reason} · {v.override_count} correction(s) · {new Date(v.created_at + "Z").toLocaleTimeString()}
              </button>
            ))}
            <div style={{ marginTop: 10 }}><b>Jobs on this dataset</b></div>
            {jobs.map((j) => (
              <button key={j.job_id} className={job?.job_id === j.job_id ? "active" : ""} onClick={() => { setJob(j); setPlan(null); setPlanDesc(null); setView({ kind: "result", rv: j.result_version_id, reviewOf: null }); location.hash = `/d/${datasetId}?rv=${j.result_version_id}`; if (j.state === "running" || j.state === "queued") follow(j); }}>
                {j.state} · {j.progress?.succeeded ?? 0} rows · ${(j.usage?.cost_usd ?? 0).toFixed(4)} · {new Date(j.created_at + "Z").toLocaleTimeString()}
              </button>
            ))}
            <div style={{ marginTop: 10 }}><b>Dataset versions</b></div>
            {dataset?.versions.map((v) => (
              <button key={v.version_id} className={view.kind === "dataset" && (view.versionId ?? dataset.current_version_id) === v.version_id ? "active" : ""} onClick={() => { setView({ kind: "dataset", versionId: v.version_id }); }}>
                v{v.number} · {v.reason} · {new Date(v.created_at + "Z").toLocaleTimeString()}
              </button>
            ))}
          </div>
        )}
      </div>
      <StatusStrip job={job} viewLabel={viewLabel} total={total} countStatus={cache.countStatus} cacheStats={cache.stats()} onCancel={cancel} localCount={localCount} />
    </div>
  );
}
