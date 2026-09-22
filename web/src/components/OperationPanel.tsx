import { useState } from "react";
import { ChevronRight, Play, RotateCcw, Square, X } from "lucide-react";
import type { ColumnInfo, Estimate, Job, Plan, ValidateResponse } from "@/types";
import { describePlan, newColumns } from "@/lib/describe";
import { cn, formatCount, formatDuration, formatDurationShort, formatUsd, jobStateLabel, jobStateVariant } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Notice, Spinner, Stat } from "@/components/ui/misc";
import { Progress } from "@/components/ui/progress";
import PlanEditor, { type Limits } from "./PlanEditor";

export type { Limits };

type Props = {
  plan: Plan | null;
  validation: ValidateResponse | null;
  validating: boolean;
  validationError: string | null;
  compileError: string | null;
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

/**
 * The operation, explained. The default view is a few sentences about what will happen, the cost, and a
 * Run button; the full step editor and safety limits sit behind “Details & edit”.
 */
export default function OperationPanel({ plan, validation, validating, validationError, compileError, schema, limits, job, budgetLeftUsd, onPlanChange, onLimitsChange, onRun, onCancel, onClear }: Props) {
  const [detailsOpen, setDetailsOpen] = useState(false);

  if (!plan) {
    return (
      <div className="flex flex-col gap-4 p-4">
        {compileError && <Notice tone="bad">{compileError}</Notice>}
        <div className="flex flex-col gap-3 pt-2">
          <p className="font-display text-[22px] leading-tight text-ink">Tell the sheet what you want.</p>
          <p className="text-[13px] leading-relaxed text-ink-muted">
            Type a request above in plain words. You will see a short explanation of what will happen before anything runs.
          </p>
          <ul className="flex flex-col gap-1.5 text-[12.5px] leading-relaxed text-ink-body">
            {["Flag the reviews that mention a safety problem.", "Sort these complaints by how urgent they sound.", "Label each ticket with a topic and count them."].map((ex) => (
              <li key={ex} className="flex gap-2">
                <span className="mt-[7px] size-1 shrink-0 rounded-full bg-ink-tertiary" />
                <span className="italic">“{ex}”</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    );
  }

  const sentences = describePlan(plan);
  const added = newColumns(plan);
  const est: Estimate | undefined = validation?.estimate;
  const running = !!job && (job.state === "queued" || job.state === "running");
  const overBudget = est ? est.estimated_cost_usd > budgetLeftUsd : false;
  const canRun = !!validation && !validating && !validationError && !running;
  const usesModel = (est?.semantic_rows ?? 0) > 0;

  const stageEntries = job ? Object.entries(job.progress.stages) : [];
  const total = stageEntries.reduce((a, [, s]) => a + s.rows_total, 0);
  const examined = job?.progress.rows_examined ?? 0;
  const pct = total ? Math.min(100, (100 * examined) / total) : running ? 5 : 100;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
        {compileError && <Notice tone="bad">{compileError}</Notice>}

        <header className="flex items-start gap-2">
          <div className="min-w-0 grow">
            <h3 className="text-[15px] font-medium tracking-[-0.01em] text-ink">{plan.title ?? "Your operation"}</h3>
          </div>
          <Button variant="ghost" size="icon-sm" onClick={onClear} aria-label="Discard this operation" title="Discard">
            <X />
          </Button>
        </header>

        <section className="flex flex-col gap-2">
          <span className="label-mono">What will happen</span>
          <ol className="flex flex-col gap-2">
            {sentences.map((s, i) => (
              <li key={s.id} className="flex gap-2.5 text-[13px] leading-relaxed text-ink-body">
                <span className="mt-[3px] inline-flex size-4.5 shrink-0 items-center justify-center rounded-full border border-line-strong font-mono text-[10px] text-ink-muted tabular-nums">{i + 1}</span>
                <div className="flex min-w-0 flex-col gap-1.5">
                  <span>{s.text}</span>
                  {s.questions?.map((q) => (
                    <div key={q.name} className="grain flex flex-col gap-0.5 rounded-md border-l-2 border-line-strong bg-field/60 py-1.5 pr-2.5 pl-3">
                      <span className="text-[12px] text-ink-muted">
                        {q.lead} <span className="font-mono text-[11px] text-ink-tertiary">→ {q.name}</span>
                      </span>
                      <span className="text-[12.5px] leading-relaxed text-ink italic">“{q.prompt}”</span>
                    </div>
                  ))}
                </div>
              </li>
            ))}
          </ol>
          {added.length > 0 && (
            <div className="flex flex-wrap items-center gap-1 pt-1 text-[12px] text-ink-muted">
              <span>New columns:</span>
              {added.map((c) => (
                <Badge key={c} variant="outline" className="normal-case tracking-normal">{c}</Badge>
              ))}
            </div>
          )}
        </section>

        <section className="grain flex flex-col gap-3 rounded-md border border-line bg-field/70 p-3">
          <div className="flex items-center gap-2">
            <span className="label-mono">Before you run</span>
            {validating && <Spinner />}
          </div>
          {validationError ? (
            <Notice tone="bad">{validationError}</Notice>
          ) : est ? (
            usesModel ? (
              <div className="grid grid-cols-3 gap-3">
                <Stat label="Rows" value={formatCount(est.semantic_rows)} hint="Rows the model will read" />
                <Stat label="Time" value={formatDurationShort(est.quota_floor_seconds)} hint={`${formatDuration(est.quota_floor_seconds)} at least, from the provider's rate limits`} />
                <Stat label="Cost" value={formatUsd(est.estimated_cost_usd)} hint={`${formatCount(est.provider_requests)} model requests, ${(est.input_tokens / 1000).toFixed(0)}k input tokens`} />
              </div>
            ) : (
              <p className="text-[12.5px] text-ink-body">No model calls needed. This runs in a moment and costs nothing.</p>
            )
          ) : (
            <p className="text-[12.5px] text-ink-muted">Checking the plan…</p>
          )}
          {overBudget && <Notice tone="warn">This would cost more than the {formatUsd(budgetLeftUsd)} left in the workspace budget. It will stop when the budget runs out.</Notice>}
          {validation?.warnings?.length ? (
            <ul className="flex flex-col gap-1 text-[12px] leading-relaxed text-warn">
              {validation.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          ) : null}
        </section>

        {job && (
          <section className="flex flex-col gap-2">
            <div className="flex items-center gap-2 text-[12.5px]">
              <Badge variant={jobStateVariant(job.state)}>{jobStateLabel(job.state)}</Badge>
              <span className="text-ink-muted">
                {running ? `${formatCount(examined)} of ${formatCount(total)} rows` : `${formatCount(examined)} rows · ${formatUsd(job.usage.spent_usd)}`}
                {job.terminal_reason && !running ? ` · ${job.terminal_reason.replace(/_/g, " ")}` : ""}
              </span>
              {running && <Spinner className="ml-auto" />}
            </div>
            <Progress value={pct} aria-label="Progress" />
            {job.error_summary && <Notice tone="bad">{job.error_summary}</Notice>}
          </section>
        )}

        <Collapsible open={detailsOpen} onOpenChange={setDetailsOpen} className="flex flex-col gap-3 border-t border-line pt-3">
          <CollapsibleTrigger className="self-start">
            Details &amp; edit
            <span className="ml-1 text-ink-tertiary">· {plan.steps.length} step{plan.steps.length === 1 ? "" : "s"}{validation ? ` · ${validation.plan_hash.slice(0, 8)}` : ""}</span>
          </CollapsibleTrigger>
          <CollapsibleContent className="flex flex-col gap-4">
            {plan.description && (
              <div className="flex flex-col gap-1">
                <span className="label-mono">Planner notes</span>
                <p className="text-[12.5px] leading-relaxed text-ink-muted">{plan.description}</p>
              </div>
            )}
            <PlanEditor plan={plan} schema={schema} limits={limits} onPlanChange={onPlanChange} onLimitsChange={onLimitsChange} />
          </CollapsibleContent>
        </Collapsible>
      </div>

      <footer className={cn("grain sticky bottom-0 flex items-center gap-3 border-t border-line bg-paper px-4 py-3")}>
        {running ? (
          <Button variant="destructive" onClick={onCancel} className="min-w-28">
            <Square className="size-3" /> Stop
          </Button>
        ) : (
          <Button size="lg" className="min-w-32" disabled={!canRun} onClick={onRun} title={overBudget ? "Estimate exceeds the remaining workspace budget" : undefined}>
            {job ? <RotateCcw /> : <Play />}
            {job ? "Run again" : "Run"}
          </Button>
        )}
        <span className="text-[12px] text-ink-muted">
          {running ? "You can keep browsing while it works." : validating ? "Checking…" : est && usesModel ? `${formatUsd(est.estimated_cost_usd)} · ${formatDuration(est.quota_floor_seconds)}` : "Nothing runs until you press Run."}
        </span>
        {!detailsOpen && !running && (
          <button type="button" className="ml-auto inline-flex items-center gap-0.5 text-[12px] text-ink-muted hover:text-ink" onClick={() => setDetailsOpen(true)}>
            Edit <ChevronRight className="size-3" />
          </button>
        )}
      </footer>
    </div>
  );
}
