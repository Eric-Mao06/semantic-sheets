import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { navigate } from "../App";
import type { DatasetSummary, SampleInfo, WorkspaceInfo } from "../types";
import { ImportDialog } from "./ImportDialog";

export function Landing({ workspace }: { workspace: WorkspaceInfo }) {
  const [datasets, setDatasets] = useState<DatasetSummary[]>([]);
  const [samples, setSamples] = useState<SampleInfo[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = () => {
    api.datasets().then((d) => setDatasets(d.datasets)).catch((e) => setError(e.message));
    api.samples().then((s) => setSamples(s.samples)).catch(() => setSamples([]));
  };
  useEffect(() => { refresh(); const t = setInterval(refresh, 4000); return () => clearInterval(t); }, []);

  const importSample = async (s: SampleInfo) => {
    setBusy(s.name); setError(null);
    try { const ds = await api.importSample(s.name); navigate(`/d/${ds.dataset_id}`); }
    catch (e: any) { setError(e.message); }
    finally { setBusy(null); }
  };

  return (
    <div className="landing">
      <header>
        <div><h1>Semantic Sheets</h1><div className="muted">Plain language → reusable operations over your tables. Jev handles judgments; code handles arithmetic.</div></div>
        <div className="muted">workspace <b>{workspace.name}</b> · spent ${workspace.spent_usd.toFixed(4)} of ${workspace.budget_usd.toFixed(2)} · {workspace.fake_jev ? "fake Jev (offline)" : workspace.model}</div>
      </header>

      <div className={"dropzone" + (drag ? " active" : "")}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files?.[0]; if (f) setFile(f); }}
        onClick={() => inputRef.current?.click()} data-testid="dropzone">
        <input ref={inputRef} type="file" accept=".csv,.tsv,.txt,.gz" onChange={(e) => { const f = e.target.files?.[0]; if (f) setFile(f); e.target.value = ""; }} />
        <div style={{ fontSize: 15, fontWeight: 600 }}>Drop a CSV or TSV here, or click to choose a file</div>
        <div className="muted">Up to {Math.round(workspace.limits.max_file_bytes / 1048576)} MiB and {workspace.limits.max_rows_per_file.toLocaleString()} rows. The original bytes are kept; identifiers stay text. Demo uploads are retained for {workspace.limits.retention_days} days.</div>
      </div>

      {error && <p className="error">{error}</p>}

      <section>
        <h2>Sample datasets</h2>
        <div className="cards">
          {samples.map((s) => (
            <div className="card" key={s.name}>
              <h3>{s.title}</h3>
              <div className="muted">{s.description}</div>
              {s.suggested_requests?.length > 0 && <div className="muted" style={{ fontStyle: "italic" }}>“{s.suggested_requests[0]}”</div>}
              <div className="actions">
                <button className="primary" disabled={busy !== null} onClick={() => importSample(s)} data-testid={`sample-${s.name}`}>{busy === s.name ? "Importing…" : "Open"}</button>
                <span className="muted">{(s.bytes / 1048576).toFixed(1)} MiB</span>
              </div>
            </div>
          ))}
          {samples.length === 0 && <div className="muted">No sample files found. Run <code>python scripts/prepare_samples.py</code> to generate them.</div>}
        </div>
      </section>

      <section>
        <h2>Your datasets</h2>
        {datasets.length === 0 ? <div className="muted">Nothing imported yet.</div> : (
          <table className="list">
            <thead><tr><th>Name</th><th>Status</th><th>Rows</th><th>Columns</th><th>Imported</th></tr></thead>
            <tbody>
              {datasets.map((d) => (
                <tr key={d.dataset_id}>
                  <td>{d.status === "ready" ? <a href={`#/d/${d.dataset_id}`}>{d.name}</a> : d.name}</td>
                  <td><span className={"pill " + (d.status === "ready" ? "ok" : d.status === "failed" ? "bad" : "pending")}>{d.status}</span></td>
                  <td>{d.row_count.toLocaleString()}</td><td>{d.column_count}</td><td className="muted">{new Date(d.created_at + "Z").toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {file && <ImportDialog file={file} onClose={() => setFile(null)} onImported={(id) => { setFile(null); navigate(`/d/${id}`); }} />}
    </div>
  );
}
