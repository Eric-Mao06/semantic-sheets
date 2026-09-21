import type { JobDescription } from "../types";

export function StatusStrip({ job, viewLabel, total, countStatus, cacheStats, onCancel, localCount }:
  { job: JobDescription | null; viewLabel: string; total: number | null; countStatus: string; cacheStats: { blocks: number; bytes: number }; onCancel: () => void; localCount: { count: number; ms: number; label: string } | null }) {
  const p = job?.progress;
  const examined = p ? p.succeeded + p.failed + p.skipped : 0;
  const frac = p && p.source_rows ? examined / p.source_rows : 0;
  const stateClass = !job ? "" : job.state === "succeeded" ? "ok" : job.state === "running" || job.state === "queued" ? "pending" : job.state === "partial" ? "warn" : "bad";
  return (
    <div className="statusstrip" data-testid="status-strip">
      <span><b>{viewLabel}</b> · {total === null ? "count unknown" : `${total.toLocaleString()} rows`} <span className="muted">({countStatus})</span></span>
      {job && (
        <>
          <span className={"pill " + stateClass} data-testid="job-state">{job.state}{job.terminal_reason ? ` · ${job.terminal_reason}` : ""}</span>
          <span className="progress" title="rows examined"><div style={{ width: `${Math.round(frac * 100)}%` }} /></span>
          <span>examined <b>{examined.toLocaleString()}</b> · remaining <b>{(p?.pending ?? 0).toLocaleString()}</b> · errors <b>{(p?.failed ?? 0).toLocaleString()}</b> · uncertain <b>{(p?.uncertain ?? 0).toLocaleString()}</b></span>
          <span>spend <b>${(job.usage?.cost_usd ?? 0).toFixed(4)}</b> · {job.usage?.requests ?? 0} req · {(p?.cache_hits ?? 0).toLocaleString()} cache hits</span>
          {(job.state === "running" || job.state === "queued") && <button onClick={onCancel} data-testid="job-cancel">Cancel</button>}
        </>
      )}
      {localCount && <span className="pill warn" data-testid="local-count">{localCount.label}: {localCount.count.toLocaleString()} rows in {localCount.ms.toFixed(1)} ms (local)</span>}
      <span className="muted" style={{ marginLeft: "auto" }}>cache {cacheStats.blocks} blocks · {(cacheStats.bytes / 1048576).toFixed(1)} MiB</span>
    </div>
  );
}
