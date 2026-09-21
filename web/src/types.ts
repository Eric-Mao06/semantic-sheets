export type ColumnInfo = { name: string; type: string; role?: string };

export type Expr =
  | { column: string }
  | { literal: string | number | boolean | null | Array<string | number | boolean | null> }
  | { op: string; args: Expr[] };

export type Question = {
  name: string;
  kind: "boolean" | "category" | "score";
  instruction: string;
  criteria?: { true?: string; false?: string } | null;
  thresholds?: { true_min: number; false_max: number };
  options?: Record<string, string | null> | null;
  min_confidence?: number;
  levels?: string[] | null;
};

export type Step =
  | { id: string; op: "semantic_annotate"; input: string; columns: string[]; questions: Question[]; on_missing?: "unknown" | "skip" }
  | { id: string; op: "filter"; input: string; where: Expr; unknown_policy?: "separate" | "exclude" | "include" }
  | { id: string; op: "sort"; input: string; by: { column: string; direction: "asc" | "desc" }[] }
  | { id: string; op: "project"; input: string; columns: string[] }
  | { id: string; op: "compute"; input: string; columns: { name: string; expr: Expr }[] }
  | { id: string; op: "aggregate"; input: string; group_by: string[]; metrics: { name: string; fn: string; column?: string | null }[] }
  | { id: string; op: "join"; input: string; right: { dataset_id: string; version_id?: string | null } | string; on: { left: string; right: string }[]; how?: "inner" | "left"; right_columns?: string[] | null; right_prefix?: string }
  | { id: string; op: "distinct"; input: string; columns?: string[] | null }
  | { id: string; op: "limit"; input: string; n: number }
  | { id: string; op: "semantic_match"; input: string; name?: string; right: { dataset_id: string; version_id?: string | null }; left_columns: string[]; right_columns: string[]; instruction: string; candidates_per_row?: number; accept_min?: number; reject_max?: number; blocking?: { left: string; right: string } | null; right_output_columns?: string[] | null; right_prefix?: string; criteria?: { true?: string; false?: string } | null };

export type Plan = {
  plan_version: "1";
  source: { dataset_id: string; version_id?: string | null };
  model: string;
  steps: Step[];
  output: string;
  title?: string | null;
  description?: string | null;
};

export type Estimate = {
  source_rows: number;
  semantic_rows: number;
  inference_attempts: number;
  provider_requests: number;
  input_tokens: number;
  estimated_cost_usd: number;
  candidate_pairs: number;
  quota_floor_seconds: number;
  stages: Record<string, unknown>[];
};

export type ValidateResponse = {
  plan_hash: string;
  plan: Plan;
  estimate: Estimate;
  warnings: string[];
  output_columns: ColumnInfo[];
  review_views: string[];
  planner?: { model: string; reasoning_effort: string; latency_ms: number; usage: Record<string, number | null> };
  title?: string | null;
  description?: string | null;
  attempts?: number;
};

export type DatasetInfo = {
  dataset_id: string;
  name: string;
  version_id: string;
  version_no: number;
  row_count: number;
  schema: (ColumnInfo & { null_count?: number; distinct_estimate?: number; max_length?: number; samples?: string[] })[];
  column_count: number;
  import_report?: { delimiter: string; header: boolean; rejected_rows: number; coercions: Record<string, unknown>; warnings: string[]; truncated_to: number | null };
  retention_expires_at?: number;
  source_filename?: string;
  versions?: { id: string; version_no: number; kind: string; row_count: number }[];
};

export type DatasetListItem = { dataset_id: string; name: string; latest_version_id: string; row_count: number; column_count: number; created_at: number; source_filename?: string };

export type StageProgress = {
  rows_total: number; rows_succeeded: number; rows_uncertain: number; rows_missing: number; rows_failed: number; rows_skipped: number; rows_pending: number;
  cache_hits: number; inference_attempts: number; provider_requests: number; pairs: number; chunks_committed: number; chunks_total: number; rows_beyond_cap?: number; complete: boolean;
};

export type Job = {
  job_id: string;
  state: "queued" | "running" | "succeeded" | "partial" | "failed" | "cancelled";
  terminal_reason: string | null;
  plan_hash: string;
  result_version_id: string;
  result_revision: number;
  progress: { stages: Record<string, StageProgress>; rows_examined: number; rows_remaining: number | null; errors: number };
  usage: { input_tokens: number; output_tokens: number; provider_requests: number; spent_usd: number; reserved_usd: number; cache_hits: number; ambiguous_attempts: number };
  limits: { max_source_rows: number; max_provider_requests: number; spend_target_usd: number; deadline_seconds: number };
  error_summary?: string | null;
  errors?: { stage_id: string; chunk_index: number; error: string }[];
  created_at: number;
  started_at?: number | null;
  finished_at?: number | null;
  cancel_requested: boolean;
};

export type JobEvent = { seq: number; type: string; [k: string]: unknown };

export type Row = Record<string, unknown> & { _row_id: number; _truncated?: string[] };

export type QueryResponse = {
  result_version_id: string;
  revision: number;
  step: string;
  query_hash: string;
  columns: ColumnInfo[];
  rows: Row[];
  start: number;
  next_start: number | null;
  has_more: boolean;
  total_count: number | null;
  count_status: "complete" | "partial" | "n/a";
  denominator: number | null;
  provisional: boolean;
  review_views: Record<string, string>;
  warnings: string[];
};

export type ResultDescribe = {
  result_version_id: string;
  job_id: string | null;
  plan: Plan;
  plan_hash: string;
  revision: number;
  status: string;
  manifest: Record<string, unknown> & { steps?: Record<string, StageProgress> };
  steps: { id: string; op: string; input: string; columns: ColumnInfo[]; provisional: boolean; review_view: string | null; row_preserving: boolean }[];
  output: string;
  parent_result_version_id: string | null;
  overrides: number;
  created_at: number;
};

export type WorkspaceInfo = { workspace_id: string; name: string; spent_usd: number; budget_usd: number; input_tokens: number; model: string; planner_model: string; retention_days: number; limits: { max_import_rows: number; default_max_source_rows: number } };

export type Sample = { key: string; title: string; description: string; suggested: string; available: boolean; bytes: number };
