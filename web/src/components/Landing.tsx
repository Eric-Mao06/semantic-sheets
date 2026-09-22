import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import Papa from "papaparse";
import { ArrowRight, Trash2, Upload } from "lucide-react";
import { api, ApiError } from "@/api";
import type { DatasetInfo, DatasetListItem, Sample } from "@/types";
import { useIsMobile } from "@/hooks/useMediaQuery";
import { cn, formatCount } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Notice, Spinner } from "@/components/ui/misc";

export type Preview = { columns: string[]; rows: unknown[][]; filename: string; bytes: number; errors: number; capped: boolean };

type Props = {
  onDataset: (ds: DatasetInfo, note?: string, suggestedPrompt?: string) => void;
  onPreview: (p: Preview | null) => void;
};

const PREVIEW_ROWS = 100;
const PREVIEW_BYTES = 512 * 1024;

export default function Landing({ onDataset, onPreview }: Props) {
  const isMobile = useIsMobile();
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
      onDataset(r.dataset, undefined, s.suggested);
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
      setUploadState({ phase: "Reading a preview…" });
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
        setUploadState({ phase: "Checking and importing…" });
        const name = file.name.replace(/\.(csv|tsv|txt|gz)+$/i, "");
        const r = await api.importDataset(prep.upload_id, name, {});
        const notes = [...(r.report.warnings ?? [])];
        if (r.report.rejected_rows) notes.push(`${r.report.rejected_rows} malformed rows were skipped`);
        onPreview(null);
        onDataset(r.dataset, notes.length ? `Imported ${formatCount(r.report.row_count)} rows. ${notes.join("; ")}` : undefined);
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
    <div className="safe-x mx-auto flex w-full max-w-4xl flex-col px-6 pt-14 pb-20 [--safe-pad:24px] max-[900px]:px-4 max-[900px]:pt-8">
      <header className="flex items-baseline gap-3">
        <span className="font-mono text-[11px] tracking-[0.08em] text-ink-secondary uppercase">Semantic Sheet</span>
      </header>

      <h1 className="mt-10 max-w-2xl font-display text-[44px] leading-[1.05] tracking-[-0.015em] text-ink max-[900px]:mt-6 max-[900px]:text-[32px]">
        Ask your spreadsheet a question. Get a column back.
      </h1>
      <p className="mt-4 max-w-xl text-[15px] leading-relaxed text-ink-muted max-[900px]:text-[14px]">
        Upload a table and say what you want in plain words: flag, label, score, sort, group or match rows. You see what will happen before it runs, and every answer can be inspected.
      </p>

      {/* Dropzone -------------------------------------------------------------------------------- */}
      <div
        role="button"
        tabIndex={0}
        className={cn(
          "grain mt-10 flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-line-strong bg-paper px-6 py-10 text-center transition-colors outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35 max-[900px]:mt-6 max-[900px]:py-7",
          drag && "border-ink bg-field",
          !uploadState && "hover:border-ink",
        )}
        onClick={() => fileRef.current?.click()}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && fileRef.current?.click()}
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
          <div className="flex items-center gap-2 text-[13.5px] text-ink">
            <Spinner /> {uploadState.phase}
          </div>
        ) : (
          <>
            <Upload className="size-4 text-ink-secondary" />
            <div className="text-[14.5px] font-medium text-ink">{isMobile ? "Tap to choose a CSV or TSV file" : "Drop a CSV or TSV here, or click to choose"}</div>
            <div className="font-mono text-[11px] tracking-[0.02em] text-ink-secondary">up to 100 MB · 100,000 rows · kept for 7 days</div>
          </>
        )}
      </div>
      {error && <Notice tone="bad" className="mt-4">{error}</Notice>}

      {/* Samples --------------------------------------------------------------------------------- */}
      <section className="mt-14 max-[900px]:mt-10">
        <SectionTitle>Try a sample</SectionTitle>
        <ul className="mt-3 divide-y divide-line border-y border-line">
          {samples.map((s) => (
            <li key={s.key} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-6 gap-y-2 py-4 max-[900px]:grid-cols-1">
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="text-[14px] font-medium text-ink">{s.title}</span>
                  <span className="font-mono text-[10.5px] tracking-[0.04em] text-ink-tertiary">{s.available ? `${(s.bytes / 1024).toFixed(0)} KB` : "not prepared"}</span>
                </div>
                <p className="mt-1 text-[13px] leading-relaxed text-ink-muted">{s.description}</p>
                {s.suggested && <p className="mt-1.5 text-[13px] leading-relaxed text-ink-body italic">“{s.suggested}”</p>}
              </div>
              <Button variant="outline" className="max-[900px]:w-full" disabled={!s.available || busy !== null} onClick={() => importSample(s)}>
                {busy === s.key ? <Spinner /> : null}
                Open <ArrowRight />
              </Button>
            </li>
          ))}
          {samples.length === 0 && !error && (
            <li className="flex items-center gap-2 py-4 text-[13px] text-ink-muted"><Spinner /> Loading samples…</li>
          )}
        </ul>
      </section>

      {/* Existing datasets ------------------------------------------------------------------------ */}
      {datasets.length > 0 && (
        <section className="mt-14 max-[900px]:mt-10">
          <SectionTitle>Your tables</SectionTitle>
          <ul className="mt-3 divide-y divide-line border-y border-line">
            {datasets.map((d) => (
              <li key={d.dataset_id} className="flex items-center gap-3 py-3">
                <div className="min-w-0 grow">
                  <div className="truncate text-[13.5px] font-medium text-ink">{d.name}</div>
                  <div className="font-mono text-[10.5px] tracking-[0.02em] text-ink-secondary">{formatCount(d.row_count)} rows · {d.column_count} columns</div>
                </div>
                <Button variant="ghost" size="sm" onClick={() => openDataset(d)} disabled={busy !== null}>
                  {busy === d.dataset_id ? <Spinner /> : null}
                  Open
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Delete ${d.name}`}
                  className="text-ink-tertiary hover:text-bad"
                  onClick={async () => {
                    if (!confirm(`Delete "${d.name}" and its results?`)) return;
                    await api.deleteDataset(d.dataset_id);
                    setDatasets((x) => x.filter((y) => y.dataset_id !== d.dataset_id));
                  }}
                >
                  <Trash2 />
                </Button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="mt-20 flex flex-wrap items-center gap-x-6 gap-y-2 font-mono text-[10.5px] tracking-[0.04em] text-ink-tertiary uppercase max-[900px]:mt-12">
        <span>Judgements run on Jev · arithmetic runs in code</span>
        <span>
          Agents: MCP at <code className="text-ink-secondary">/mcp</code>
        </span>
      </footer>
    </div>
  );
}

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <h2 className="flex items-baseline gap-2 font-mono text-[11px] tracking-[0.08em] text-ink-secondary uppercase">
      {children}
      <span className="text-ink-tertiary" aria-hidden>#</span>
    </h2>
  );
}
