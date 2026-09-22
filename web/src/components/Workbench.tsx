import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowLeft, ChevronDown, CornerDownLeft, Filter, X } from "lucide-react";
import { api, subscribeJobEvents } from "@/api";
import { ViewController, type ViewSpec } from "@/data/ViewController";
import type { ColumnInfo, DatasetInfo, Job, JobEvent, Plan, Question, ResultDescribe, Row, ValidateResponse, WorkspaceInfo } from "@/types";
import { useIsMobile } from "@/hooks/useMediaQuery";
import { opLabel } from "@/lib/describe";
import { cn, formatCount, formatUsd, jobStateLabel } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Kbd, Notice, Spinner } from "@/components/ui/misc";
import { Progress } from "@/components/ui/progress";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toast } from "@/components/ui/toast";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import Grid from "./Grid";
import History, { type ExportInfo } from "./History";
import Inspector from "./Inspector";
import OperationPanel, { type Limits } from "./OperationPanel";
import QuickFilter, { type LocalFilterResult } from "./QuickFilter";
import type { Preview } from "./Landing";

type Props = { dataset: DatasetInfo; workspace: WorkspaceInfo; initialPrompt?: string; note?: string; preview?: Preview | null; onBack: () => void; onWorkspaceRefresh: () => void };

type SideTab = "plan" | "inspect" | "history";
/** On small screens the table and the side panel are shown one at a time and switched with the bottom navigation. */
type MobilePane = "table" | "panel";

export default function Workbench({ dataset, workspace, initialPrompt, note, preview, onBack, onWorkspaceRefresh }: Props) {
  const isMobile = useIsMobile();
  const [mobilePane, setMobilePane] = useState<MobilePane>("table");
  const lastTap = useRef<{ row: number; col: string } | null>(null);
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
  const [exportInfo, setExportInfo] = useState<ExportInfo>(null);
  const [localFilter, setLocalFilter] = useState<LocalFilterResult | null>(null);
  const [filterOpen, setFilterOpen] = useState(false);
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

  useEffect(() => {
    if (note) window.setTimeout(() => setToast((t) => (t === note ? null : t)), 6000);
  }, [note]);

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
  const openPanel = useCallback((tab: SideTab) => {
    setSideTab(tab);
    setMobilePane("panel");
  }, []);

  const compile = async () => {
    if (!prompt.trim()) return;
    setCompiling(true);
    setCompileError(null);
    setSideTab("plan");
    (document.activeElement as HTMLElement | null)?.blur?.(); // dismiss the on-screen keyboard while planning
    try {
      const r = await api.compile(dataset.dataset_id, dataset.version_id, prompt, plan);
      setPlan(r.plan);
      setValidation(r);
      setValidationError(null);
    } catch (e) {
      setCompileError((e as Error).message);
    } finally {
      setCompiling(false);
      // The plan (or the planner error) lives in the side panel, which is a separate pane on phones.
      if (isMobile) setMobilePane("panel");
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
              const outcome = jobStateLabel(jj.state);
              showToast(`${outcome[0].toUpperCase()}${outcome.slice(1)} · ${formatUsd(jj.usage.spent_usd)}`);
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
      setMobilePane("table"); // watch rows fill in as chunks commit
    } catch (e) {
      showToast(`Could not start: ${(e as Error).message}`);
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
      showToast("Saved. The model's original answer is kept in History.");
    } catch (e) {
      setPendingEdits((m) => {
        const n = new Map(m);
        n.delete(`${rowId}:${column}`);
        return n;
      });
      showToast(`Could not save: ${(e as Error).message}`);
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
      setExportInfo(await api.export(activeRv, format, step === "source" ? undefined : step, raw));
    } catch (e) {
      showToast(`Export failed: ${(e as Error).message}`);
    }
  };

  // ---- derived ---------------------------------------------------------------------------------
  const meta = controller.meta;
  const stageEntries = job ? Object.entries(job.progress.stages) : [];
  const examined = job?.progress.rows_examined ?? 0;
  const totalRows = stageEntries.reduce((a, [, s]) => a + s.rows_total, 0);
  const stepInfo = result?.steps.find((s) => s.id === step);
  const isRunning = !!job && (job.state === "queued" || job.state === "running");
  const budgetLeft = Math.max(0, wsInfo.budget_usd - wsInfo.spent_usd);
  const hasResult = !!result?.job_id;
  const reviewViews = hasResult ? result!.steps.filter((s) => s.review_view) : [];
  const intermediate = hasResult ? result!.steps.filter((s) => s.id !== result!.output) : [];
  const viewingIntermediate = hasResult && step !== "source" && step !== result!.output && !step.endsWith("__review");

  const pickStep = (id: string) => {
    userPickedStep.current = true;
    setLocalFilter(null);
    setUpdatesAvailable(false);
    setStep(id);
  };

  const onCellClick = (idx: number, col: ColumnInfo) => {
    const row = controller.getRow(idx) ?? null;
    setSelected({ row, column: col });
    if (!isMobile) {
      setSideTab("inspect");
      return;
    }
    // Phones: the first tap selects and shows the chip; tapping the same cell again opens the inspector.
    const key = { row: idx, col: col.name };
    if (lastTap.current && lastTap.current.row === key.row && lastTap.current.col === key.col) openPanel("inspect");
    lastTap.current = key;
  };

  const showSide = !isMobile || mobilePane === "panel";
  const showTable = !isMobile || mobilePane === "table";

  const viewTab = (id: string, label: string, extra?: ReactNode) => (
    <button
      type="button"
      key={id}
      onClick={() => pickStep(id)}
      className={cn("-mb-px inline-flex h-9 items-center gap-1.5 border-b-[1.5px] px-0.5 text-[12.5px] whitespace-nowrap transition-colors", step === id ? "border-ink text-ink" : "border-transparent text-ink-muted hover:text-ink")}
    >
      {label}
      {extra}
    </button>
  );

  return (
    <TooltipProvider>
      <div className={cn("grid h-full grid-rows-[48px_auto_minmax(0,1fr)] overflow-hidden", isMobile && "grid-rows-[44px_auto_minmax(0,1fr)_auto] overscroll-contain")}>
        {/* Top bar ---------------------------------------------------------------------------- */}
        <header className="safe-x flex min-w-0 items-center gap-3 border-b border-line bg-paper px-3 [--safe-pad:12px]">
          <Button variant="ghost" size="sm" onClick={onBack} aria-label="Back to your tables">
            <ArrowLeft />
            {!isMobile && "Tables"}
          </Button>
          <div className="min-w-0">
            <div className="truncate text-[13.5px] font-medium text-ink" title={dataset.name}>{dataset.name}</div>
            <div className="truncate font-mono text-[10.5px] tracking-[0.02em] text-ink-secondary">
              {formatCount(dataset.row_count)} rows · {dataset.column_count} columns
              {dataset.import_report?.rejected_rows ? ` · ${dataset.import_report.rejected_rows} rows skipped` : ""}
              {preview ? " · preview" : ""}
            </div>
          </div>
          <span className="grow" />
          {!isMobile && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="font-mono text-[11px] tracking-[0.02em] text-ink-secondary tabular-nums">
                  spent {formatUsd(wsInfo.spent_usd)} <span className="text-ink-tertiary">/ {formatUsd(wsInfo.budget_usd)}</span>
                </span>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                Workspace spend. Judgements run on {wsInfo.model}; requests are planned by {wsInfo.planner_model}.
              </TooltipContent>
            </Tooltip>
          )}
        </header>

        {/* Command bar ------------------------------------------------------------------------ */}
        <div className="safe-x border-b border-line bg-paper px-3 py-2.5 [--safe-pad:12px]">
          <form
            className="relative flex items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (!compiling) void compile();
            }}
          >
            <Input
              className="h-10 flex-1 rounded-md bg-page pr-24 text-[14px] shadow-none"
              placeholder={isMobile ? "What do you want to do with this table?" : "What do you want to do with this table? e.g. “Find customers trying to cancel because they can't afford it, and rate how urgent each message is”"}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              disabled={compiling}
              enterKeyHint="go"
              autoCapitalize="sentences"
              aria-label="Describe what you want to do"
            />
            <div className="absolute top-1/2 right-1.5 flex -translate-y-1/2 items-center gap-1.5">
              {prompt && !compiling && !isMobile && <Kbd className="text-ink-tertiary"><CornerDownLeft className="size-2.5" /></Kbd>}
              <Button type="submit" size="sm" disabled={compiling || !prompt.trim()} className="h-7">
                {compiling ? <Spinner className="text-white" /> : null}
                {compiling ? "Thinking…" : plan ? "Update" : "Go"}
              </Button>
            </div>
          </form>
        </div>

        {/* Main ------------------------------------------------------------------------------ */}
        <div className={cn("grid min-h-0", isMobile ? "grid-cols-[minmax(0,1fr)]" : "grid-cols-[minmax(0,1fr)_420px] max-[1200px]:grid-cols-[minmax(0,1fr)_380px]")}>
          <div className="flex min-h-0 min-w-0 flex-col" hidden={!showTable}>
            {/* Views row */}
            <div className="flex h-9 items-center gap-4 border-b border-line bg-paper px-3 scrollbar-none overflow-x-auto">
              {viewTab("source", "Source")}
              {hasResult && viewTab(result!.output, "Result", stepInfo?.provisional && step === result!.output ? <Badge variant="pending">updating</Badge> : undefined)}
              {reviewViews.map((s) => viewTab(s.review_view!, reviewViews.length > 1 ? `Needs a look · ${s.id}` : "Needs a look"))}
              {intermediate.length > 0 && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button type="button" className={cn("-mb-px inline-flex h-9 items-center gap-1 border-b-[1.5px] px-0.5 text-[12.5px] whitespace-nowrap", viewingIntermediate ? "border-ink text-ink" : "border-transparent text-ink-muted hover:text-ink")}>
                      {viewingIntermediate ? `Step · ${step}` : "Steps"}
                      <ChevronDown className="size-3" />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start">
                    <DropdownMenuLabel>Intermediate steps</DropdownMenuLabel>
                    {intermediate.map((s) => (
                      <DropdownMenuItem key={s.id} onSelect={() => pickStep(s.id)}>
                        <span className="font-mono text-[11.5px]">{s.id}</span>
                        <span className="ml-auto text-ink-muted">{opLabel(s.op)}</span>
                      </DropdownMenuItem>
                    ))}
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onSelect={() => pickStep(result!.output)}>Back to result</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
              <span className="grow" />
              {meta.total !== null && (
                <span className="font-mono text-[11px] tracking-[0.02em] whitespace-nowrap text-ink-secondary tabular-nums">
                  {formatCount(meta.total)} rows{meta.denominator !== null && meta.denominator !== meta.total ? ` of ${formatCount(meta.denominator)}` : ""}
                  {meta.countStatus === "partial" ? " · so far" : ""}
                </span>
              )}
              {hasResult && stepInfo?.row_preserving && !step.endsWith("__review") && (
                <Button variant={filterOpen || localFilter ? "secondary" : "ghost"} size="sm" className="h-6" onClick={() => setFilterOpen((o) => !o)} aria-expanded={filterOpen}>
                  <Filter /> Filter{localFilter && <span className="ml-0.5 size-1.5 rounded-full bg-blue" aria-label="filter on" />}
                </Button>
              )}
            </div>
            {hasResult && stepInfo && stepInfo.row_preserving && (
              <QuickFilter rv={activeRv!} step={step} columns={columns} revision={result!.revision} enabled={!step.endsWith("__review")} open={filterOpen} onResult={setLocalFilter} />
            )}

            {/* Grid */}
            <div className="relative min-h-0 flex-1 bg-paper">
              {updatesAvailable && (
                <Button size="sm" className="absolute top-2.5 right-4 z-10 shadow-md" onClick={() => { setUpdatesAvailable(false); userPickedStep.current = false; if (result) setStep(result.output); setLocalFilter(null); }}>
                  New rows ready · show result
                </Button>
              )}
              {activeRv && columns.length > 0 ? (
                <Grid controller={controller} columns={columns} dataVersion={dataVersion} editable={editable} pendingEdits={pendingEdits} compact={isMobile} onCellClick={onCellClick} onEdit={(rowId, column, value) => void correct(rowId, column, value, "inline edit")} />
              ) : (
                <div className="pointer-events-none absolute inset-0 flex items-center justify-center gap-2 text-[12.5px] text-ink-muted"><Spinner /> Loading…</div>
              )}
              {meta.error && <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-6"><Notice tone="bad">{meta.error}</Notice></div>}
              {isMobile && selected.row && selected.column && (
                <div className="grain absolute right-2.5 bottom-2.5 left-2.5 z-10 flex items-center gap-2 rounded-md border border-line bg-paper px-2.5 py-2 shadow-lg">
                  <span className="truncate font-mono text-[11.5px] text-ink">{selected.column.name}</span>
                  <span className="text-[11.5px] text-ink-muted">row {selected.row._row_id}</span>
                  <span className="grow" />
                  <Button size="sm" onClick={() => openPanel("inspect")}>Look</Button>
                  <Button variant="ghost" size="icon-sm" aria-label="Dismiss" onClick={() => { setSelected({ row: null, column: null }); lastTap.current = null; }}><X /></Button>
                </div>
              )}
            </div>

            {/* Job progress: a single quiet line, only while a job exists. */}
            {job && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <div className="flex h-7 items-center gap-3 border-t border-line bg-paper px-3 text-[11.5px] text-ink-muted">
                    <Progress className="w-28 shrink-0" value={totalRows ? Math.min(100, (100 * examined) / totalRows) : isRunning ? 5 : 100} aria-label="Job progress" />
                    <span className="truncate tabular-nums">
                      {isRunning ? `Working… ${formatCount(examined)} of ${formatCount(totalRows)} rows` : `${jobStateLabel(job.state)[0].toUpperCase()}${jobStateLabel(job.state).slice(1)} · ${formatCount(examined)} rows`}
                      {" · "}{formatUsd(job.usage.spent_usd)}
                      {job.progress.errors ? ` · ${job.progress.errors} errors` : ""}
                    </span>
                    {isRunning && <Spinner className="ml-auto" />}
                  </div>
                </TooltipTrigger>
                <TooltipContent side="top" align="start" className="font-mono text-[11px]">
                  {stageEntries.map(([id, s]) => (
                    <div key={id}>{id}: {s.rows_succeeded} ok · {s.rows_uncertain} unsure{s.rows_flagged ? ` · ${s.rows_flagged} near the cut` : ""} · {s.rows_missing} missing{s.rows_beyond_cap ? ` · ${s.rows_beyond_cap} beyond cap` : ""}</div>
                  ))}
                  <div>{job.usage.provider_requests} requests · {job.usage.cache_hits} cached · {(job.usage.input_tokens / 1000).toFixed(1)}k tokens</div>
                </TooltipContent>
              </Tooltip>
            )}
          </div>

          {/* Side panel ------------------------------------------------------------------------ */}
          <aside className={cn("flex min-h-0 flex-col bg-paper", !isMobile && "border-l border-line")} hidden={!showSide}>
            <Tabs value={sideTab} onValueChange={(v) => setSideTab(v as SideTab)} className="min-h-0 flex-1">
              {!isMobile && (
                <TabsList>
                  <TabsTrigger value="plan">Operation{(plan || compileError) && <span className={cn("size-1.5 rounded-full", compileError ? "bg-bad" : "bg-blue")} />}</TabsTrigger>
                  <TabsTrigger value="inspect">Cell{selected.row && <span className="size-1.5 rounded-full bg-ink-tertiary" />}</TabsTrigger>
                  <TabsTrigger value="history">History</TabsTrigger>
                </TabsList>
              )}
              <TabsContent value="plan" className="flex min-h-0 flex-col data-[state=inactive]:hidden">
                <OperationPanel plan={plan} validation={validation} validating={validating} validationError={validationError} compileError={compileError} schema={dataset.schema} limits={limits} job={job} budgetLeftUsd={budgetLeft} onPlanChange={onPlanChange} onLimitsChange={(l) => { setLimits(l); if (plan) onPlanChange(plan); }} onRun={run} onCancel={cancel} onClear={() => { setPlan(null); setValidation(null); setCompileError(null); }} />
              </TabsContent>
              <TabsContent value="inspect" className="flex min-h-0 flex-col data-[state=inactive]:hidden">
                {activeRv && <Inspector rv={activeRv} step={step} row={selected.row} column={selected.column} semanticStep={semanticStepForColumn(selected.column).stepId} questions={semanticStepForColumn(selected.column).questions} onCorrect={correct} />}
              </TabsContent>
              <TabsContent value="history" className="flex min-h-0 flex-col data-[state=inactive]:hidden">
                <History rv={hasResult ? activeRv : null} activeRv={activeRv} jobs={jobs} onSelect={(rv) => { userPickedStep.current = false; setLocalFilter(null); setActiveRv(rv); setJob(jobs.find((j) => j.result_version_id === rv) ?? null); setMobilePane("table"); }} onExport={doExport} exportInfo={exportInfo} />
              </TabsContent>
            </Tabs>
          </aside>
        </div>

        {/* Bottom navigation (phones) --------------------------------------------------------- */}
        {isMobile && (
          <nav className="flex border-t border-line bg-paper pb-[env(safe-area-inset-bottom,0px)]" aria-label="Sections">
            {(
              [
                ["table", "Table", isRunning],
                ["plan", "Operation", !!(plan || compileError)],
                ["inspect", "Cell", !!selected.row],
                ["history", "History", false],
              ] as [string, string, boolean][]
            ).map(([id, label, dot]) => {
              const active = id === "table" ? mobilePane === "table" : mobilePane === "panel" && sideTab === id;
              return (
                <button
                  key={id}
                  type="button"
                  className={cn("inline-flex min-h-12 flex-1 items-center justify-center gap-1.5 border-t-[1.5px] text-[12.5px] font-medium", active ? "border-ink text-ink" : "border-transparent text-ink-muted")}
                  onClick={() => (id === "table" ? setMobilePane("table") : openPanel(id as SideTab))}
                >
                  {label}
                  {dot && <span className={cn("size-1.5 rounded-full", id === "table" ? "animate-pulse bg-blue" : compileError && id === "plan" ? "bg-bad" : "bg-blue")} />}
                </button>
              );
            })}
          </nav>
        )}
        {toast && <Toast className={isMobile ? "bottom-[calc(64px+env(safe-area-inset-bottom,0px))]" : undefined}>{toast}</Toast>}
      </div>
    </TooltipProvider>
  );
}
