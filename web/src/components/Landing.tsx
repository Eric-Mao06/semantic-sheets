import { useCallback, useEffect, useRef, useState } from "react";
import Papa from "papaparse";
import { api, ApiError } from "../api";
import type { DatasetInfo, DatasetListItem, Sample } from "../types";

export type Preview = { columns: string[]; rows: unknown[][]; filename: string; bytes: number; errors: number; capped: boolean };

type Props = {
  onDataset: (ds: DatasetInfo, note?: string) => void;
  onPreview: (p: Preview | null) => void;
};

const PREVIEW_ROWS = 100;
const PREVIEW_BYTES = 512 * 1024;

export default function Landing({ onDataset, onPreview }: Props) {
  const [samples, setSamples] = useState<Sample[]>([]);
  const [datasets, setDatasets] = useState<DatasetListItem[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [drag, setDrag] = useState(false);
  const [uploadState, setUploadState] = useState<{ phase: string; pct?: number } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.samples().then((r) => setSamples(r.samples)).catch((e) => setError(String(e.message)));
    api.datasets().then((r) => setDatasets(r.datasets)).catch(() => {});
  }, []);

  const importSample = async (s: Sample) => {
    setBusy(s.key);
    setError(null);
    try {
      const r = await api.importSample(s.key);
      onDataset(r.dataset, s.suggested);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const openDataset = async (d: DatasetListItem) => {
    setBusy(d.dataset_id);
    try {
      onDataset(await api.dataset(d.dataset_id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const handleFile = useCallback(
    async (file: File) => {
      setError(null);
      setUploadState({ phase: "Parsing preview…" });
      // 1. Bounded provisional preview in a worker; painted immediately, labelled provisional.
      const previewPromise = new Promise<Preview>((resolve) => {
        const rows: unknown[][] = [];
        let errors = 0;
        let columns: string[] = [];
        Papa.parse<string[]>(new File([file.slice(0, PREVIEW_BYTES)], file.name), {
          worker: true,
          preview: PREVIEW_ROWS + 1,
          skipEmptyLines: true,
          step: (res) => {
            if (res.errors?.length) errors += res.errors.length;
            if (!columns.length) columns = res.data as string[];
            else rows.push(res.data);
          },
          complete: () => resolve({ columns, rows, filename: file.name, bytes: file.size, errors, capped: file.size > PREVIEW_BYTES }),
          error: () => resolve({ columns, rows, filename: file.name, bytes: file.size, errors: errors + 1, capped: true }),
        });
      });
      previewPromise.then(onPreview);
      // 2. Upload the original bytes directly and run the canonical server import.
      try {
        setUploadState({ phase: "Uploading…" });
        const prep = await api.uploadPrepare(file.name);
        await api.uploadContent(prep.upload_id, file);
        setUploadState({ phase: "Validating and importing…" });
        const name = file.name.replace(/\.(csv|tsv|txt|gz)+$/i, "");
        const r = await api.importDataset(prep.upload_id, name, {});
        const notes = [...(r.report.warnings ?? [])];
        if (r.report.rejected_rows) notes.push(`${r.report.rejected_rows} malformed rows rejected`);
        onPreview(null);
        onDataset(r.dataset, notes.length ? `Imported ${r.report.row_count.toLocaleString()} rows. ${notes.join("; ")}` : undefined);
      } catch (e) {
        onPreview(null);
        const err = e as ApiError;
        const details = err.details as { rejected_rows?: number; rejects?: { line: number; error: string }[]; row_count?: number; cap?: number } | undefined;
        let msg = err.message;
        if (details?.rejects?.length) msg += ` First problems: ${details.rejects.slice(0, 3).map((r) => `line ${r.line}: ${r.error}`).join(" | ")}`;
        setError(msg);
      } finally {
        setUploadState(null);
      }
    },
    [onDataset, onPreview],
  );

  return (
    <div className="landing">
      <h1>Semantic Sheet</h1>
      <p className="lede">
        Upload a table and describe an operation in plain language: classify feedback, score records, filter by meaning, rank results, group themes or match rows across files.
        Judgements run on Jev; arithmetic, joins and aggregates run in code. The same operations are available to agents through MCP at <code>/mcp</code>.
      </p>

      <div
        className={"dropzone" + (drag ? " active" : "")}
        onClick={() => fileRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          const f = e.dataTransfer.files?.[0];
          if (f) void handleFile(f);
        }}
      >
        <input ref={fileRef} type="file" accept=".csv,.tsv,.txt,.gz" hidden onChange={(e) => e.target.files?.[0] && void handleFile(e.target.files[0])} />
        {uploadState ? (
          <div className="row" style={{ justifyContent: "center" }}>
            <span className="spinner" /> {uploadState.phase}
          </div>
        ) : (
          <>
            <div style={{ fontSize: 16, fontWeight: 600 }}>Drop a CSV or TSV here, or click to choose</div>
            <div className="muted" style={{ marginTop: 6 }}>Up to 100 MiB and 100,000 rows. Identifiers with leading zeros stay text. Demo uploads are retained for 7 days.</div>
          </>
        )}
      </div>
      {error && <div className="error" style={{ marginTop: 12 }}>{error}</div>}

      <div className="section-title">Sample datasets</div>
      <div className="cards">
        {samples.map((s) => (
          <div className="card" key={s.key}>
            <h3>{s.title}</h3>
            <p>{s.description}</p>
            {s.suggested && <div className="suggest">“{s.suggested}”</div>}
            <div className="actions">
              <span className="muted">{s.available ? `${(s.bytes / 1024).toFixed(0)} KB` : "not prepared"}</span>
              <button className="btn primary" disabled={!s.available || busy !== null} onClick={() => importSample(s)}>
                {busy === s.key ? <span className="spinner" /> : "Open"}
              </button>
            </div>
          </div>
        ))}
      </div>

      {datasets.length > 0 && (
        <>
          <div className="section-title">Your datasets</div>
          <div className="dataset-list">
            {datasets.map((d) => (
              <div className="item" key={d.dataset_id}>
                <div className="grow">
                  <b>{d.name}</b> <span className="muted">· {d.row_count.toLocaleString()} rows · {d.column_count} columns</span>
                </div>
                <button className="btn small" onClick={() => openDataset(d)} disabled={busy !== null}>Open</button>
                <button
                  className="btn small danger"
                  onClick={async () => {
                    if (!confirm(`Delete "${d.name}" and its results?`)) return;
                    await api.deleteDataset(d.dataset_id);
                    setDatasets((x) => x.filter((y) => y.dataset_id !== d.dataset_id));
                  }}
                >
                  Delete
                </button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
