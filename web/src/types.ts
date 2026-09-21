export type LogicalType = "text" | "integer" | "number" | "boolean" | "date" | "timestamp" | "json";
export interface ColumnDef { name: string; type: LogicalType | string; }
export interface SchemaColumn extends ColumnDef { null_count?: number; distinct_estimate?: number; max_length?: number; examples?: unknown[]; }

export interface DatasetSummary { dataset_id: string; name: string; status: string; row_count: number; column_count: number; created_at: string; current_version_id: string | null; }
export interface DatasetDescription {
  dataset_id: string; name: string; status: string; row_count: number; schema: SchemaColumn[];
  quality: Record<string, unknown> & { warnings?: string[]; rejected_rows?: number };
  parse_options: Record<string, unknown>; source: { filename: string; bytes: number; checksum: string };
  current_version_id: string | null; versions: { version_id: string; number: number; parent_id: string | null; reason: string; created_at: string }[];
  retention_until: string | null; created_at: string; error?: { code: string; message: string; details?: unknown }; sample?: Record<string, unknown>[];
}

export interface Thresholds { true_min: number; false_max: number; }
export interface Label { name: string; description?: string | null; }
export type Question =
  | { name: string; kind: "boolean"; instruction: string; thresholds?: Thresholds; criteria?: Record<string, string | null> | null }
  | { name: string; kind: "category"; instruction: string; labels: Label[]; add_default_labels?: boolean; min_confidence?: number }
  | { name: string; kind: "score"; instruction: string; levels: string[] };

export type Step =
  | { id: string; op: "semantic_annotate"; input: string; columns: string[]; questions: Question[]; on_missing?: "unknown" | "skip" }
  | { id: string; op: "filter"; input: string; where: Expr; unknown_policy?: "separate" | "exclude" | "include" }
  | { id: string; op: "sort"; input: string; by: { column: string; direction: "asc" | "desc" }[] }
  | { id: string; op: "project"; input: string; columns: string[] }
  | { id: string; op: "derive"; input: string; columns: { name: string; expr: Expr }[] }
  | { id: string; op: "aggregate"; input: string; group_by: string[]; metrics: { name: string; fn: string; column?: string | null }[] }
  | { id: string; op: "join"; input: string; right: { dataset_id: string; version_id?: string | null }; on: { left: string; right: string }[]; how?: string; right_columns?: string[] | null; prefix?: string }
  | { id: string; op: "dedupe"; input: string; keys: string[] }
  | { id: string; op: "limit"; input: string; n: number }
  | { id: string; op: "semantic_match"; input: string; right: { dataset_id: string; version_id?: string | null }; left_columns: string[]; right_columns: string[]; instruction: string; candidates_per_row?: number; thresholds?: { match_min: number; nonmatch_max: number }; mode?: "best" | "all"; show_right_columns?: string[] | null; blocking?: { left: string; right: string }[]; max_right_rows?: number };

export type Expr = Record<string, unknown>;
export interface Plan { plan_version: "1"; source: { dataset_id: string; version_id?: string | null }; model?: string | null; steps: Step[]; output: string; }

export interface Estimate { source_rows: number; semantic_rows: number; provider_requests: number; input_tokens: number; candidate_pairs: number; cost_usd: number; model: string; semantic_steps: string[]; }
export interface PlanDescription {
  plan_hash: string; plan: Plan; estimate: Estimate; warnings: string[]; required_scopes: string[];
  steps: { id: string; op: string; semantic: boolean; columns: Record<string, string>; estimate: Record<string, unknown> }[];
  output_columns: Record<string, string>; plan_version_id?: string; nl_request?: string | null;
  planner?: { summary?: string; assumptions?: string[]; suggested_name?: string; usage?: Record<string, number>; model?: string; reasoning_effort?: string; repairs?: number };
}

export interface StepProgress { source_rows: number; succeeded: number; failed: number; skipped: number; pending: number; uncertain: number; cache_hits: number; pairs: number; chunks_total: number; chunks_done: number; complete: boolean; }
export interface JobProgress extends StepProgress { steps?: Record<string, StepProgress>; }
export interface Usage { requests: number; input_tokens: number; output_tokens: number; cost_usd: number; provider_errors: number; retries: number; }
export interface JobDescription {
  job_id: string; state: "queued" | "running" | "succeeded" | "partial" | "failed" | "cancelled"; terminal_reason: string | null;
  dataset_id: string; dataset_version_id: string; plan_version_id: string; model: string; limits: Record<string, number>;
  progress: JobProgress; usage: Usage; reserved_usd: number; result_version_id: string; result_revision: number;
  errors: { kind: string; message: string; at: string }[]; error_count: number; cancel_requested: boolean; attempts: number;
  created_at: string; started_at: string | null; finished_at: string | null; suggested_poll_seconds: number; complete: boolean; created?: boolean;
}

export interface Completion { job_state: string; complete: boolean; semantic_steps: string[]; steps: Record<string, Partial<StepProgress>>; source_rows?: number; succeeded?: number; failed?: number; skipped?: number; pending?: number; uncertain?: number; revision: number; }
export interface QueryResponse {
  view_id?: string; view_revision: number; result_version_id: string; columns: ColumnDef[]; rows: unknown[][]; row_ordinals?: number[];
  start?: number; returned: number; next_start?: number; has_more: boolean; total_count?: number; count_status: "complete" | "partial";
  provisional: boolean; completion: Completion; truncated_cells: number; bytes: number; denominator: Record<string, unknown>; note?: string;
}
export interface ResultColumns { result_version_id: string; revision: number; output: string; columns: ColumnDef[]; steps: { id: string; op: string; semantic: boolean; columns: string[] }[]; completion: Completion; job_id: string; job_state: string; plan_version_id: string; dataset_version_id: string; }
export interface ResultVersionInfo { result_version_id: string; number: number; parent_id: string | null; reason: string; override_count: number; created_at: string; }
export interface ExportInfo { export_id: string; status: string; format: string; bytes: number; row_count: number; manifest: Record<string, unknown>; download_path: string; download_url: string; }
export interface SampleInfo { name: string; title: string; description: string; bytes: number; suggested_requests: string[]; source: string; }
export interface WorkspaceInfo { workspace_id: string; name: string; scopes: string[]; budget_usd: number; spent_usd: number; reserved_usd: number; model: string; planner_model: string; limits: Record<string, number>; fake_jev: boolean; }
