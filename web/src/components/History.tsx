import { useEffect, useState } from "react";
import { ArrowUpRight, ChevronDown, Download } from "lucide-react";
import { api } from "@/api";
import type { Job } from "@/types";
import { cn, formatCount, formatUsd, jobStateLabel, jobStateVariant } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Notice, Spinner } from "@/components/ui/misc";

type V = { result_version_id: string; parent_result_version_id: string | null; status: string; created_at: number; overrides: number; note?: string };

export type ExportInfo = { download_url: string; manifest_url: string; size: number; row_count: number; complete: boolean; formula_escaped: boolean } | null;

type Props = {
  rv: string | null;
  activeRv: string | null;
  jobs: Job[];
  onSelect: (rv: string) => void;
  onExport: (format: "csv" | "parquet", raw: boolean) => Promise<void>;
  exportInfo: ExportInfo;
};

function when(ts: number): string {
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** Past runs on this table, hand edits (only when there are any), and export. */
export default function History({ rv, activeRv, jobs, onSelect, onExport, exportInfo }: Props) {
  const [versions, setVersions] = useState<V[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!rv) return setVersions([]);
    api.versions(rv).then((r) => setVersions(r.versions)).catch(() => setVersions([]));
  }, [rv, activeRv]);

  const run = async (format: "csv" | "parquet", raw: boolean) => {
    setBusy(true);
    try {
      await onExport(format, raw);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-5 overflow-y-auto p-4">
      <section className="flex flex-col gap-2">
        <span className="label-mono">Export this view</span>
        <div className="flex items-center gap-1.5">
          <Button variant="outline" disabled={!activeRv || busy} onClick={() => run("csv", false)}>
            {busy ? <Spinner /> : <Download />} Download CSV
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon" disabled={!activeRv || busy} aria-label="More formats"><ChevronDown /></Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuItem onSelect={() => run("csv", true)}>CSV without formula escaping</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => run("parquet", false)}>Parquet</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        {exportInfo && (
          <Notice tone="ok" className="flex flex-col gap-1.5">
            <div>{formatCount(exportInfo.row_count)} rows · {(exportInfo.size / 1024).toFixed(1)} KB{exportInfo.complete ? "" : " · partial result"}</div>
            <div className="flex gap-2">
              <a className="inline-flex items-center gap-1 text-link hover:underline" href={api.downloadUrl(exportInfo.download_url)} target="_blank" rel="noreferrer">
                Download file <ArrowUpRight className="size-3" />
              </a>
              <a className="inline-flex items-center gap-1 text-ink-muted hover:underline" href={api.downloadUrl(exportInfo.manifest_url)} target="_blank" rel="noreferrer">
                Manifest
              </a>
            </div>
          </Notice>
        )}
      </section>

      <section className="flex flex-col gap-2">
        <span className="label-mono">Runs on this table</span>
        {jobs.length === 0 && <p className="text-[12.5px] text-ink-muted">Nothing has run yet.</p>}
        <ul className="flex flex-col divide-y divide-line rounded-md border border-line bg-paper">
          {jobs.slice(0, 12).map((j) => {
            const active = j.result_version_id === activeRv;
            return (
              <li key={j.job_id} className={cn("flex items-center gap-3 px-3 py-2 text-[12.5px]", active && "bg-field/70")}>
                <Badge variant={jobStateVariant(j.state)}>{jobStateLabel(j.state)}</Badge>
                <div className="min-w-0 grow">
                  <div className="truncate text-ink">{when(j.created_at)}</div>
                  <div className="truncate text-[11.5px] text-ink-muted">
                    {formatUsd(j.usage.spent_usd)} · {formatCount(j.usage.provider_requests)} requests
                    {j.terminal_reason ? ` · ${j.terminal_reason.replace(/_/g, " ")}` : ""}
                  </div>
                </div>
                {active ? <span className="label-mono">viewing</span> : <Button variant="ghost" size="sm" onClick={() => onSelect(j.result_version_id)}>Open</Button>}
              </li>
            );
          })}
        </ul>
      </section>

      {versions.length > 1 && (
        <section className="flex flex-col gap-2">
          <span className="label-mono">Hand edits</span>
          <p className="text-[12px] leading-relaxed text-ink-muted">Each edit creates a new version. Opening an earlier one undoes the later edits.</p>
          <ul className="flex flex-col divide-y divide-line rounded-md border border-line bg-paper">
            {versions.map((v, i) => {
              const active = v.result_version_id === activeRv;
              return (
                <li key={v.result_version_id} className={cn("flex items-center gap-3 px-3 py-2 text-[12.5px]", active && "bg-field/70")}>
                  <Badge variant="outline">{i === 0 ? "original" : `edit ${i}`}</Badge>
                  <div className="min-w-0 grow">
                    <div className="truncate text-ink">{v.note ?? (i === 0 ? "Model output" : "Correction")}</div>
                    <div className="truncate text-[11.5px] text-ink-muted">{when(v.created_at)} · {v.overrides} change{v.overrides === 1 ? "" : "s"}</div>
                  </div>
                  {active ? <span className="label-mono">viewing</span> : <Button variant="ghost" size="sm" onClick={() => onSelect(v.result_version_id)}>Open</Button>}
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
