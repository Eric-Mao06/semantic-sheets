import { useEffect, useRef, useState } from "react";
import { api } from "../api";

interface Preview { fields: string[]; rows: unknown[][]; errors: number; delimiter?: string; bytesRead: number; ms: number; provisional: boolean; error?: string; }

/** Local provisional preview in a worker while the original file uploads; then a server import with full validation. */
export function ImportDialog({ file, onClose, onImported }: { file: File; onClose: () => void; onImported: (datasetId: string) => void }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [uploadFrac, setUploadFrac] = useState(0);
  const [uploadId, setUploadId] = useState<string | null>(null);
  const [serverPreview, setServerPreview] = useState<{ columns: { name: string; type: string }[]; rows: unknown[][]; delimiter: string; header: boolean } | null>(null);
  const [name, setName] = useState(file.name.replace(/\.(csv|tsv|txt)(\.gz)?$/i, ""));
  const [permissive, setPermissive] = useState(false);
  const [allText, setAllText] = useState(false);
  const [delimiter, setDelimiter] = useState("");
  const [status, setStatus] = useState<"uploading" | "uploaded" | "importing" | "failed">("uploading");
  const [error, setError] = useState<{ message: string; details?: any } | null>(null);
  const [datasetId, setDatasetId] = useState<string | null>(null);
  const workerRef = useRef<Worker | null>(null);

  useEffect(() => {
    const w = new Worker(new URL("../data/preview.worker.ts", import.meta.url), { type: "module" });
    workerRef.current = w;
    w.onmessage = (e) => setPreview(e.data);
    w.postMessage({ file, maxRows: 100, maxBytes: 2 * 1024 * 1024 });
    (async () => {
      try {
        const prep = await api.prepareUpload(file.name);
        await api.uploadBytes(prep.upload_id, file, setUploadFrac);
        setUploadId(prep.upload_id); setStatus("uploaded");
        const sp = await api.previewUpload(prep.upload_id);
        setServerPreview(sp);
      } catch (e: any) { setStatus("failed"); setError({ message: e.message, details: e.details }); }
    })();
    return () => w.terminate();
  }, [file]);

  useEffect(() => {
    if (!datasetId) return;
    const t = setInterval(async () => {
      try {
        const d = await api.dataset(datasetId);
        if (d.status === "ready") { clearInterval(t); onImported(datasetId); }
        else if (d.status === "failed") { clearInterval(t); setStatus("failed"); setError({ message: d.error?.message || "import failed", details: d.error?.details }); }
      } catch (e: any) { clearInterval(t); setStatus("failed"); setError({ message: e.message }); }
    }, 700);
    return () => clearInterval(t);
  }, [datasetId, onImported]);

  const startImport = async () => {
    if (!uploadId) return;
    setStatus("importing"); setError(null);
    try {
      const options: Record<string, unknown> = { permissive, all_text: allText };
      if (delimiter) options.delimiter = delimiter === "\\t" ? "\t" : delimiter;
      const r = await api.importDataset(uploadId, name, options);
      setDatasetId(r.dataset_id);
    } catch (e: any) { setStatus("failed"); setError({ message: e.message, details: e.details }); }
  };

  const cols = serverPreview?.columns ?? (preview?.fields ?? []).map((f) => ({ name: f, type: "?" }));
  const rows = serverPreview?.rows ?? preview?.rows ?? [];

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Import {file.name} <span className="muted">({(file.size / 1048576).toFixed(2)} MiB)</span></h2>
        <div className="row" style={{ marginBottom: 8 }}>
          <label>Name <input value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label>Delimiter <input style={{ width: 60 }} value={delimiter} onChange={(e) => setDelimiter(e.target.value)} placeholder="auto" /></label>
          <label><input type="checkbox" checked={permissive} onChange={(e) => setPermissive(e.target.checked)} /> permissive (skip malformed rows)</label>
          <label><input type="checkbox" checked={allText} onChange={(e) => setAllText(e.target.checked)} /> all columns as text</label>
        </div>
        <div className="row" style={{ marginBottom: 8 }}>
          <div style={{ flex: 1 }}>
            <div className="muted">Upload {status === "uploading" ? `${Math.round(uploadFrac * 100)}%` : "complete"} · {serverPreview ? `server sniff: delimiter ${JSON.stringify(serverPreview.delimiter)}, header ${String(serverPreview.header)}` : preview ? `local preview (provisional): ${preview.rows.length} rows in ${preview.ms.toFixed(0)} ms` : "parsing preview…"}</div>
            <div className="progress"><div style={{ width: `${Math.round(uploadFrac * 100)}%` }} /></div>
          </div>
          <button className="primary" disabled={status !== "uploaded"} onClick={startImport} data-testid="import-start">{status === "importing" ? "Validating on server…" : "Import"}</button>
          <button onClick={onClose}>Cancel</button>
        </div>
        {error && (
          <div className="error">
            {error.message}
            {error.details?.error_report && datasetId && <> · <a href={`/api/v1/datasets/${datasetId}/import-errors?token=${encodeURIComponent(localStorage.getItem("ss.workspace_key") || "")}`}>download error report</a></>}
          </div>
        )}
        {preview?.error && <div className="error">{preview.error}</div>}
        <div style={{ overflow: "auto", maxHeight: "50vh", marginTop: 8 }}>
          <table className="preview-table">
            <thead><tr>{cols.map((c, i) => <th key={i}>{c.name} <span className="muted">{c.type}</span></th>)}</tr></thead>
            <tbody>{rows.slice(0, 50).map((r, i) => <tr key={i}>{cols.map((_, j) => <td key={j} title={String(r[j] ?? "")}>{String(r[j] ?? "")}</td>)}</tr>)}</tbody>
          </table>
        </div>
        <div className="muted" style={{ marginTop: 6 }}>{serverPreview ? "Types shown are from the server sniff; the full file is validated on import." : "This preview is provisional; the server validates the full file and assigns stable row IDs."}</div>
      </div>
    </div>
  );
}
