import { useEffect, useState } from "react";
import { api } from "../api";
import type { Job } from "../types";

type V = { result_version_id: string; parent_result_version_id: string | null; status: string; created_at: number; overrides: number; note?: string };

type Props = {
  rv: string | null;
  activeRv: string | null;
  jobs: Job[];
  onSelect: (rv: string) => void;
  onExport: (format: "csv" | "parquet", raw: boolean) => Promise<void>;
  exportInfo: { download_url: string; manifest_url: string; size: number; row_count: number; complete: boolean; formula_escaped: boolean } | null;
};

export default function Versions({ rv, activeRv, jobs, onSelect, onExport, exportInfo }: Props) {
  const [versions, setVersions] = useState<V[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!rv) return setVersions([]);
    api.versions(rv).then((r) => setVersions(r.versions)).catch(() => setVersions([]));
  }, [rv, activeRv]);

  return (
    <div className="body versions">
      <div>
        <h3 style={{ marginBottom: 6 }}>Result versions</h3>
        <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>Every job creates an immutable result. Corrections create a child version; selecting an earlier version is undo.</div>
        {versions.length === 0 && <div className="muted">Run an operation to create a result.</div>}
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {versions.map((v, i) => (
            <div className={"item" + (v.result_version_id === activeRv ? " active" : "")} key={v.result_version_id}>
              <span className="badge">{i === 0 ? "job result" : `v${i + 1}`}</span>
              <div className="grow" style={{ fontSize: 12.5 }}>
                <div>{v.note ?? (i === 0 ? "model output" : "correction")}</div>
                <div className="muted">{new Date(v.created_at * 1000).toLocaleTimeString()} · {v.overrides} override{v.overrides === 1 ? "" : "s"} · {v.status}</div>
              </div>
              {v.result_version_id !== activeRv && <button className="btn small" onClick={() => onSelect(v.result_version_id)}>Select</button>}
            </div>
          ))}
        </div>
      </div>

      <div>
        <h3 style={{ marginBottom: 6 }}>Jobs on this dataset</h3>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {jobs.slice(0, 8).map((j) => (
            <div className={"item" + (j.result_version_id === activeRv ? " active" : "")} key={j.job_id}>
              <span className={"badge " + (j.state === "succeeded" ? "ok" : j.state === "partial" ? "warn" : j.state === "failed" ? "bad" : j.state === "running" ? "accent" : "")}>{j.state}</span>
              <div className="grow" style={{ fontSize: 12.5 }}>
                <div className="mono">{j.job_id.slice(-8)} · ${j.usage.spent_usd.toFixed(4)} · {j.usage.provider_requests} req · {j.usage.cache_hits} cached</div>
                <div className="muted">{new Date(j.created_at * 1000).toLocaleTimeString()}{j.terminal_reason ? ` · ${j.terminal_reason}` : ""}</div>
              </div>
              <button className="btn small" onClick={() => onSelect(j.result_version_id)}>Open</button>
            </div>
          ))}
          {jobs.length === 0 && <div className="muted">No jobs yet.</div>}
        </div>
      </div>

      <div>
        <h3 style={{ marginBottom: 6 }}>Export</h3>
        <div className="row wrap">
          <button className="btn" disabled={!activeRv || busy} onClick={async () => { setBusy(true); try { await onExport("csv", false); } finally { setBusy(false); } }}>CSV (escaped)</button>
          <button className="btn" disabled={!activeRv || busy} onClick={async () => { setBusy(true); try { await onExport("csv", true); } finally { setBusy(false); } }}>CSV (raw)</button>
          <button className="btn" disabled={!activeRv || busy} onClick={async () => { setBusy(true); try { await onExport("parquet", false); } finally { setBusy(false); } }}>Parquet</button>
        </div>
        {exportInfo && (
          <div className="warnings" style={{ marginTop: 8, background: "#eef7f0", borderColor: "#cfe9d6" }}>
            <div>{exportInfo.row_count.toLocaleString()} rows · {(exportInfo.size / 1024).toFixed(1)} KB · {exportInfo.complete ? "complete" : "partial result"}{exportInfo.formula_escaped ? " · formula-escaped" : ""}</div>
            <div className="row">
              <a className="btn small" href={api.downloadUrl(exportInfo.download_url)} target="_blank" rel="noreferrer">Download file</a>
              <a className="btn small" href={api.downloadUrl(exportInfo.manifest_url)} target="_blank" rel="noreferrer">Manifest</a>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
