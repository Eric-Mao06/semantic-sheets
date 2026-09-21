import type { DatasetDescription, DatasetSummary, ExportInfo, JobDescription, Plan, PlanDescription, QueryResponse, ResultColumns, ResultVersionInfo, SampleInfo, WorkspaceInfo } from "./types";

const KEY_STORAGE = "ss.workspace_key";

export function getKey(): string {
  try {
    return localStorage.getItem(KEY_STORAGE) || (import.meta.env.VITE_WORKSPACE_KEY as string | undefined) || "";
  } catch {
    return (import.meta.env.VITE_WORKSPACE_KEY as string | undefined) || "";
  }
}
export function setKey(k: string): void { localStorage.setItem(KEY_STORAGE, k); }

export class ApiError extends Error {
  code: string; status: number; path?: string; details?: unknown; requestId?: string;
  constructor(status: number, body: any, requestId?: string) {
    const err = body?.error || {};
    super(err.message || body?.detail?.[0]?.msg || `HTTP ${status}`);
    this.status = status; this.code = err.code || "http_error"; this.path = err.path; this.details = err.details; this.requestId = requestId;
  }
}

async function call<T>(method: string, path: string, body?: unknown, signal?: AbortSignal, raw?: BodyInit): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${getKey()}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api/v1${path}`, { method, headers, body: raw ?? (body !== undefined ? JSON.stringify(body) : undefined), signal });
  const rid = res.headers.get("x-request-id") || undefined;
  if (!res.ok) {
    let parsed: any = null;
    try { parsed = await res.json(); } catch { /* ignore */ }
    throw new ApiError(res.status, parsed, rid);
  }
  return (await res.json()) as T;
}

export const api = {
  workspace: () => call<WorkspaceInfo>("GET", "/workspace"),
  samples: () => call<{ samples: SampleInfo[] }>("GET", "/samples"),
  importSample: (name: string) => call<DatasetDescription>("POST", `/samples/${encodeURIComponent(name)}/import`),
  datasets: () => call<{ datasets: DatasetSummary[] }>("GET", "/datasets?limit=100"),
  dataset: (id: string, sample = false) => call<DatasetDescription>("GET", `/datasets/${id}?sample=${sample}`),
  datasetRows: (id: string, start: number, limit: number, columns?: string[], versionId?: string | null, signal?: AbortSignal) =>
    call<{ columns: string[]; rows: unknown[][]; total_count: number; count_status: string; has_more: boolean; truncated_cells: number }>(
      "GET", `/datasets/${id}/rows?start=${start}&limit=${limit}${columns ? `&columns=${encodeURIComponent(columns.join(","))}` : ""}${versionId ? `&version_id=${versionId}` : ""}`, undefined, signal),
  datasetCell: (id: string, rowId: number, column: string, versionId?: string | null) =>
    call<{ value: unknown }>("GET", `/datasets/${id}/cell?row_id=${rowId}&column=${encodeURIComponent(column)}${versionId ? `&version_id=${versionId}` : ""}`),
  datasetPatch: (id: string, corrections: { row_id: number; column: string; value: unknown }[], versionId?: string | null) =>
    call<{ version_id: string; number: number }>("POST", `/datasets/${id}/patch`, { version_id: versionId ?? null, corrections, provenance: { source: "sheet" } }),
  setVersion: (id: string, versionId: string) => call<{ current_version_id: string }>("POST", `/datasets/${id}/current-version`, { version_id: versionId }),
  deleteDataset: (id: string) => call<{ status: string }>("DELETE", `/datasets/${id}`),
  datasetJobs: (id: string) => call<{ jobs: JobDescription[] }>("GET", `/datasets/${id}/jobs`),
  prepareUpload: (filename: string) => call<{ upload_id: string; upload_path: string; max_bytes: number }>("POST", "/uploads/prepare", { filename }),
  uploadBytes: async (uploadId: string, file: File, onProgress?: (frac: number) => void) =>
    new Promise<{ upload_id: string; bytes: number }>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", `/api/v1/uploads/${uploadId}`);
      xhr.setRequestHeader("Authorization", `Bearer ${getKey()}`);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onload = () => { if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText)); else { let b: any = null; try { b = JSON.parse(xhr.responseText); } catch { /* */ } reject(new ApiError(xhr.status, b)); } };
      xhr.onerror = () => reject(new Error("upload failed"));
      xhr.send(file);
    }),
  previewUpload: (uploadId: string, options?: Record<string, unknown>) =>
    call<{ delimiter: string; header: boolean; columns: { name: string; type: string }[]; rows: unknown[][] }>("POST", "/uploads/preview", { upload_id: uploadId, options }),
  importDataset: (uploadId: string, name?: string, options?: Record<string, unknown>) =>
    call<{ dataset_id: string; status: string; name: string }>("POST", "/datasets/import", { upload_id: uploadId, name, options, wait: false }),
  compile: (datasetId: string, request: string, currentPlan?: Plan | null) =>
    call<PlanDescription>("POST", "/plans/compile", { dataset_id: datasetId, request, current_plan: currentPlan ?? null }),
  validate: (plan: Plan) => call<PlanDescription>("POST", "/plans/validate", { plan }),
  submit: (body: { plan?: Plan; plan_hash?: string; limits?: Record<string, number>; idempotency_key?: string }) => call<JobDescription>("POST", "/jobs", body),
  job: (id: string) => call<JobDescription>("GET", `/jobs/${id}`),
  cancel: (id: string) => call<JobDescription>("POST", `/jobs/${id}/cancel`),
  resultColumns: (rv: string) => call<ResultColumns>("GET", `/results/${rv}`),
  query: (rv: string, spec: Record<string, unknown>, signal?: AbortSignal) => call<QueryResponse>("POST", `/results/${rv}/query`, spec, signal),
  resultCell: (rv: string, rowId: number, column: string) => call<{ value: unknown }>("GET", `/results/${rv}/cell?row_id=${rowId}&column=${encodeURIComponent(column)}`),
  rowDetail: (rv: string, rowId: number) => call<{ row_id: number; values: Record<string, unknown>; columns: Record<string, string>; overrides: { column: string; value: unknown; provenance: Record<string, unknown>; created_at: string }[] }>("GET", `/results/${rv}/rows/${rowId}`),
  vectors: (rv: string, columns: string[]) => call<{ revision: number; row_ids: number[]; columns: Record<string, { type: string; values: unknown[] }>; completion: { complete: boolean } }>("GET", `/results/${rv}/vectors?columns=${encodeURIComponent(columns.join(","))}`),
  versions: (rv: string) => call<{ job_id: string; versions: ResultVersionInfo[] }>("GET", `/results/${rv}/versions`),
  resultPatch: (rv: string, corrections: { row_id: number; column: string; value: unknown }[]) =>
    call<{ result_version_id: string; number: number }>("POST", `/results/${rv}/patch`, { corrections, provenance: { source: "sheet" } }),
  export: (body: { result_version_id: string; format: "csv" | "parquet"; raw?: boolean; scope?: Record<string, unknown> }) => call<ExportInfo>("POST", "/exports", body),
  downloadUrl: (exportId: string) => `/api/v1/exports/${exportId}/download?token=${encodeURIComponent(getKey())}`,
};

export type JobEvent = { seq: number; kind: string; [k: string]: unknown };

/** Subscribe to sequenced job events; the server replays after `after` and ends on `finished`. */
export function subscribeJob(jobId: string, after: number, onEvent: (e: JobEvent) => void, onError?: (e: Event) => void): () => void {
  let closed = false;
  let lastSeq = after;
  let es: EventSource | null = null;
  let retry = 0;
  const open = () => {
    if (closed) return;
    // EventSource cannot set headers; the key travels as a query param over the loopback/dev proxy.
    es = new EventSource(`/api/v1/jobs/${jobId}/events?after=${lastSeq}&token=${encodeURIComponent(getKey())}`);
    const handler = (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data) as JobEvent;
        if (typeof data.seq === "number") lastSeq = Math.max(lastSeq, data.seq);
        onEvent(data);
        if (data.kind === "finished") { closed = true; es?.close(); }
      } catch { /* ignore */ }
    };
    for (const k of ["queued", "running", "step_started", "progress", "step_finished", "finished"]) es.addEventListener(k, handler as EventListener);
    es.onerror = (e) => {
      es?.close();
      if (!closed) { retry += 1; onError?.(e); setTimeout(open, Math.min(5000, 300 * 2 ** retry)); }
    };
  };
  open();
  return () => { closed = true; es?.close(); };
}
