import type { DatasetInfo, DatasetListItem, Job, JobEvent, Plan, QueryResponse, ResultDescribe, Sample, ValidateResponse, WorkspaceInfo } from "./types";

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
let token = localStorage.getItem("semsheet.token") || "demo-token";

export function setToken(t: string) {
  token = t;
  localStorage.setItem("semsheet.token", t);
}
export function getToken() {
  return token;
}

export class ApiError extends Error {
  code: string;
  status: number;
  details: unknown;
  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function req<T>(method: string, path: string, body?: unknown, signal?: AbortSignal, rawBody?: BodyInit): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(BASE + path, { method, headers, body: rawBody ?? (body !== undefined ? JSON.stringify(body) : undefined), signal });
  if (!res.ok) {
    let err: { code?: string; message?: string; details?: unknown } = {};
    try {
      const j = await res.json();
      err = j.error ?? j.detail ?? j;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, err.code ?? "http_error", err.message ?? `HTTP ${res.status}`, err.details);
  }
  return (await res.json()) as T;
}

export const api = {
  workspace: () => req<WorkspaceInfo>("GET", "/api/workspace"),
  samples: () => req<{ samples: Sample[] }>("GET", "/api/samples"),
  importSample: (key: string) => req<{ dataset: DatasetInfo; report: unknown }>("POST", `/api/samples/${key}/import`, {}),
  datasets: () => req<{ datasets: DatasetListItem[] }>("GET", "/api/datasets"),
  dataset: (id: string, versionId?: string) => req<DatasetInfo>("GET", `/api/datasets/${id}${versionId ? `?version_id=${versionId}` : ""}`),
  deleteDataset: (id: string) => req<unknown>("DELETE", `/api/datasets/${id}`),
  uploadPrepare: (filename: string) => req<{ upload_id: string; upload_url: string }>("POST", "/api/uploads", { filename }),
  uploadContent: (uploadId: string, file: File) => req<{ upload_id: string; size: number }>("PUT", `/api/uploads/${uploadId}/content`, undefined, undefined, file),
  importDataset: (uploadId: string, name: string, options: Record<string, unknown> = {}) =>
    req<{ dataset: DatasetInfo; report: { row_count: number; warnings: string[]; rejected_rows: number; coercions: Record<string, unknown> } }>("POST", "/api/datasets/import", { upload_id: uploadId, name, options }),
  baseQuery: (datasetId: string, body: Record<string, unknown>) => req<QueryResponse>("POST", `/api/datasets/${datasetId}/query`, body),
  compile: (datasetId: string, versionId: string, prompt: string, previousPlan?: Plan | null) =>
    req<ValidateResponse>("POST", "/api/plans/compile", { dataset_id: datasetId, version_id: versionId, prompt, previous_plan: previousPlan ?? null }),
  validate: (plan: Plan, limits?: Record<string, unknown>) => req<ValidateResponse>("POST", "/api/plans/validate", { plan, limits }),
  submit: (planHash: string, limits: Record<string, unknown>, idempotencyKey: string) => req<Job>("POST", "/api/jobs", { plan_hash: planHash, limits, idempotency_key: idempotencyKey }),
  job: (id: string) => req<Job>("GET", `/api/jobs/${id}`),
  jobs: (datasetVersionId?: string) => req<{ jobs: Job[] }>("GET", `/api/jobs${datasetVersionId ? `?dataset_version_id=${datasetVersionId}` : ""}`),
  cancel: (id: string) => req<Job>("POST", `/api/jobs/${id}/cancel`, {}),
  describeResult: (rv: string) => req<ResultDescribe>("GET", `/api/results/${rv}`),
  query: (rv: string, body: Record<string, unknown>, signal?: AbortSignal) => req<QueryResponse>("POST", `/api/results/${rv}/query`, body, signal),
  cell: (rv: string, step: string, rowId: number, column: string) => req<{ value: unknown }>("GET", `/api/results/${rv}/cell?step=${encodeURIComponent(step)}&row_id=${rowId}&column=${encodeURIComponent(column)}`),
  provenance: (rv: string, step: string, rowId: number) => req<{ raw: Record<string, unknown>; overrides: unknown[]; model: string; questions: unknown[] }>("GET", `/api/results/${rv}/provenance?step=${encodeURIComponent(step)}&row_id=${rowId}`),
  vectors: (rv: string, step: string, columns: string[]) =>
    req<{ row_ids: number[]; revision: number; complete: boolean; columns: Record<string, { kind: "number" | "label"; values: (number | null)[]; labels?: string[]; type: string }> }>("POST", `/api/results/${rv}/vectors`, { step, columns }),
  patch: (rv: string, overrides: { row_id: number; column: string; value: unknown; reason?: string }[], note?: string) =>
    req<{ result_version_id: string }>("POST", `/api/results/${rv}/patch`, { overrides, note }),
  versions: (rv: string) => req<{ versions: { result_version_id: string; parent_result_version_id: string | null; status: string; created_at: number; overrides: number; note?: string }[] }>("GET", `/api/results/${rv}/versions`),
  export: (rv: string, format: "csv" | "parquet", step?: string, raw = false) =>
    req<{ export_id: string; download_url: string; manifest_url: string; size: number; row_count: number; complete: boolean; formula_escaped: boolean }>("POST", `/api/results/${rv}/export`, { format, step, raw }),
  downloadUrl: (path: string) => `${BASE}${path}?token=${encodeURIComponent(token)}`,
};

/** Sequenced job events over SSE with replay after reconnect. */
export function subscribeJobEvents(jobId: string, onEvent: (ev: JobEvent) => void, onDone: () => void): () => void {
  let lastSeq = 0;
  let closed = false;
  let es: EventSource | null = null;
  const open = () => {
    if (closed) return;
    es = new EventSource(`${BASE}/api/jobs/${jobId}/events?after=${lastSeq}&token=${encodeURIComponent(token)}`);
    es.onmessage = (m) => handle(m);
    for (const t of ["queued", "running", "stage_started", "chunk_committed", "stage_completed", "finished", "cancelled", "cancel_requested"]) {
      es.addEventListener(t, (m) => handle(m as MessageEvent));
    }
    es.onerror = () => {
      es?.close();
      if (!closed) setTimeout(open, 1000);
    };
  };
  const handle = (m: MessageEvent) => {
    try {
      const ev = JSON.parse(m.data) as JobEvent;
      if (ev.seq <= lastSeq) return;
      lastSeq = ev.seq;
      onEvent(ev);
      if (ev.type === "finished" || ev.type === "cancelled") {
        closed = true;
        es?.close();
        onDone();
      }
    } catch {
      /* ignore malformed */
    }
  };
  open();
  return () => {
    closed = true;
    es?.close();
  };
}
