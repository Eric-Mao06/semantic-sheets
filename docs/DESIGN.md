Semantic Spreadsheet MVP Design
Spreadsheet Frontend and MCP Execution Architecture
Draft for product and engineering review • 21 September 2026
## Product proposal
Build a spreadsheet that turns plain language into reusable operations over uploaded tables. Users can classify feedback, score records, filter by meaning, rank results, group themes, and match related rows across files. The same execution system is available through MCP so frontier models can delegate bulk processing to Jev and ordinary code.
The central design choice is to keep the dataset on the server. A frontier model works with the schema, a small sample, a typed plan, and compact results. Jev performs the supported semantic judgments; code handles arithmetic, joins on keys, sorting, and aggregation. Neither interface requires the frontier model to read every row.
## Decisions proposed in this draft
| Area | MVP decision |
| Interfaces | A web spreadsheet and a remote MCP server use one plan format, job system, permission model, and result store. |
| Semantic backend | Use a dedicated Jev adapter for boolean judgments, categories, and rubric scores. Pin the model version on every run. |
| Launch scope | CSV and TSV import; a Glide-based sheet with worker-assisted filtering; derived columns, ranking, aggregation, and bounded matching; CSV and Parquet export. |
| Scale | Target 300,000 rows or 100 MiB per file after validation, with a configurable 100,000-row initial cap. Use 1,000–10,000 short rows for the inference demo. |
| Cost control | Estimate before execution, enforce row and request limits, reserve spend before dispatch, and never silently switch to a frontier model. |
| Product promise | Fast first results, visible full-dataset progress, and instant reuse of completed scores. Full scans remain subject to provider quotas. |
## Why this is useful
The useful unit of work is a whole table operation: “Find onboarding complaints from paying customers, rank severity, and show the accounts affected.” A person can refine that operation in the grid; an agent can include the same result in a larger workflow without bringing the table into its context window.
All limits, service targets, and implementation choices below are proposed defaults. Published provider constraints are identified separately and linked in the references. This document specifies a build; it does not report an implemented or benchmarked product.
# 2  User experience and scope
## The first session
The landing screen offers an upload control and three sample datasets. After import, the user sees the original table, detected column types, row count, and a command bar. The system flags parsing problems before semantic processing and preserves identifiers such as leading-zero account codes as text.
For a request such as “Add issue type and severity, then show serious onboarding complaints,” the planner reads the schema and at most 20 bounded sample rows. It produces editable operation steps showing the input columns, categories or rubric, missing-data rule, and estimated cost. The user can run within a preauthorized workspace budget without approving individual calls.
Completed chunks appear in the grid as the job runs. A status strip shows rows examined, rows remaining, errors, and spend. Filtered views display the scan denominator. Rankings and aggregates are marked provisional until their required inputs are complete. A cancelled job remains inspectable and exportable as a partial result.
## Editing and reuse
The operation panel is the source of truth for the workflow. Editing a threshold, sort order, or score weight reuses stored predictions. Editing a rubric or source value invalidates affected predictions and downstream results. Undo selects an earlier version. A manual correction creates an explicit override with its own provenance and never alters the original model output.
| Sample | Natural language operation | Visible result |
| Customer feedback | Classify themes and severity; filter onboarding issues; group affected accounts. | New columns, a ranked view, and exact counts of distinct accounts. |
| Agent evaluations | Check whether responses follow a supplied policy and identify unsupported claims. | Evaluation columns and inspectable source text for review. |
| Product catalogs | Match supplier descriptions to an internal catalog and retain uncertain pairs. | Candidate matches, scores, and an unmatched review queue. |
## Scope boundaries
Launch with CSV and TSV input, preserved source files, reusable views, version history, row corrections, job cancellation, and export. Treat XLSX import of cell values as the next ingestion milestone; formula execution and spreadsheet formatting are outside the initial build. Keep the execution layer columnar so Parquet import can follow without changing operator contracts.
The MVP does not browse the web, enrich arbitrary records, execute user code, or generate a paragraph for every row. Free text extraction and narrative synthesis require a separate generative stage with a visible budget. For launch, users supply labels or accept a small taxonomy suggested during planning; Jev then applies that fixed taxonomy to the table.
# 3  Operator contracts
Every operation declares its source version, input columns, output types, missing-data behavior, and completion scope. Semantic outputs retain the raw prediction alongside the interpreted value. LOTUS supplies the relational model for this interface; a dedicated backend translates supported operations into Jev primitives. [1–4]
| Operation | Execution and returned value | Meaning and limits |
| Classify | Jev Choice returns one label and its probability distribution. | Fixed, described labels with “other” and “insufficient evidence” options. Multi-label tagging uses separate boolean questions. |
| Semantic filter | Jev Noul yields a yes probability; code applies configured thresholds. | Three outcomes: match, non-match, uncertain. Missing input is a separate status. Default uncertain band is provisional and task-calibrated. |
| Score | Jev Score returns a position on an ordered, described rubric. | Persist the rubric and raw distribution. A likelihood of “severe” is different from a severity score. |
| Rank | Score eligible rows, then sort numerically with stable row IDs as the tie breaker. | Expose this as score-based ranking. It is not equivalent to LOTUS pairwise top-k and cannot claim global completeness from a shortlist. |
| Group and aggregate | Classify into a fixed taxonomy; code groups, counts, sums, and computes statistics. | Exact arithmetic over model-assigned groups. Discovering a taxonomy and synthesizing prose are separate generative tasks. [11] |
| Semantic match | Generate candidate pairs; Jev scores or verifies their relation; code applies selection rules. | Candidate-limited matching is approximate. Return multiple candidates or abstain. Exhaustive mode is available only within the pair budget. [10] |
| Exact operations | Typed expressions perform selection, arithmetic, dates, key joins, deduplication, and aggregation. | Run in code with explicit null and join semantics. Do not use model judgments for numerical computation. |
## Important semantics
Filtering preserves unknown and failed rows in a review view rather than silently treating them as false. A model distribution is a decision signal, not a calibrated guarantee. Suggested thresholds must be evaluated on representative labeled examples before being presented as reliable. [3, 5]
For revenue analysis, first derive the distinct affected account IDs and join to a table with one authoritative revenue value per account. Summing the revenue repeated on every feedback row overcounts the same account. Show the entity and denominator beside every aggregate.
Each semantic match returns candidate provenance, a relation score, and coverage metadata. A failed search for a candidate means “no match found among these candidates,” not proof that no match exists anywhere in the reference table.
# 4  Architecture and data model
Use a small service architecture with separate API and worker processes. A React and TypeScript grid talks to an application API. A Python service exposes both that API and an MCP adapter. Both adapters call the same plan validation, scheduling, query, and export services. Business logic must not live in the UI or in MCP tool handlers.
| Component | Responsibility |
| Planner | Converts user language and bounded schema context into a typed plan. An MCP caller can supply the plan directly and avoid a second planning model. |
| Compiler and exact engine | Validate a versioned operation graph, resolve columns, estimate work, and execute supported expressions with DuckDB over Arrow or Parquet data. |
| Scheduler and workers | Persist queued jobs, enforce shared quotas and budgets, dispatch Jev requests, checkpoint results, and publish progress. |
| Storage | Postgres holds workspace metadata, plans, jobs, permissions, and manifests. Object storage holds source files and immutable columnar data. |
| Result services | Serve grid pages, bounded MCP queries, provenance, version history, and streamed export files from the same result snapshots. |
## Durable objects
A Dataset has a workspace owner, schema, source checksum, and stable row IDs assigned at import. A DatasetVersion identifies an immutable snapshot. A PlanVersion records typed operations and dependencies. A Job binds a plan to specific inputs, model versions, and budgets. A ResultVersion references base data plus derived columns and completion metadata.
Each prediction records the row ID, operation ID, model version, normalized input hash, prompt and rubric versions, raw response, status, and usage allocation. Corrections are separate override records. Materialized views retain their input versions so the same question can be audited later.
## Import and query behavior
Import samples the file to propose delimiter and types, then validates the full parse. Keep the original bytes and record coercion failures. Reject malformed rows with a downloadable error report or import them under an explicit permissive mode. Never silently truncate long cells to make inference fit.
The grid requests small pages and projected columns. Large sorts and aggregates run on the server. Generated columns are stored separately from the base table to avoid copying the full upload after each operation. Use a constrained expression tree for calculations and filters; neither interface accepts arbitrary Python or unrestricted SQL.
## Relationship to LOTUS
Reuse LOTUS terminology and evaluate its optimizations where compatible. Do not make the MVP depend on replacing a generic chat-model client with Jev: the typed API and operator meanings need explicit adapters. Pairwise ranking and prose aggregation remain separate future execution strategies. [1, 2, 6]
# 5  Frontend implementation choice
Use Glide Data Grid as the proposed sheet renderer, backed by a paged data service and a Web Worker for compact local calculations. Keep React responsible for controls and panels. Rendering, fetching, and query execution have separate budgets: a smooth viewport does not by itself make a full-table filter fast.
We reviewed the implementations below in their primary documentation. Large-row examples establish useful design patterns, but their performance claims are not measurements of this application. The recommendation remains subject to the representative benchmark on page 9.
| Implementation | Evidence and tradeoff | Decision for this MVP |
| Glide Data Grid | MIT-licensed canvas grid with lazy cell rendering, editing, and selection. Filtering and sorting belong to the application data source. [12] | Preferred for a dense sheet with text, labels, and score cells. Build the view controller and test keyboard and screen-reader behavior. |
| AG Grid Enterprise | Virtualizes rows and columns; its Server-Side Row Model fetches and caches blocks and delegates table operations to a server. Production requires a license. [15–17] | Strong alternative when built-in grouping, range selection, clipboard behavior, and established server adapters save substantial engineering work. |
| AG Grid Community | Provides core sorting, filtering, editing, and virtualization. Enterprise adds the Server-Side Row Model and richer spreadsheet features. [17] | Suitable for a simpler grid; do not assume the Enterprise feature set is available for free. |
| TanStack Table with Virtual | Headless table logic with separate virtualization. Its docs describe examples around 100,000 rows and explicitly qualify results by data size and complexity. [18, 19] | Useful for custom report tables. Building sheet editing, range selection, keyboard behavior, and render tuning adds work here. |
## Why Glide fits this product
The semantic engine already owns filtering, ranking, and aggregation, so an application-controlled row model is a useful fit. Canvas keeps dense cells out of the React component tree. Use standard text, number, and badge rendering in the sheet; put rich evidence, markdown, and editing controls in a DOM side panel or active-cell overlay.
Before committing, compare Glide and AG Grid on the same data adapter, viewport, and interactions. Include editing, copy and paste, IME input, keyboard navigation, screen-reader inspection, and browser zoom. If Glide fails required usability or performance gates, select AG Grid with the required license and feature set. This is a build recommendation, not an independently verified speed ranking.
# 6  Loading and the browser data model
## Show useful data before the complete file is ready
On file selection, paint the sheet shell immediately and parse a bounded preview in a worker while uploading the original File directly. Papa Parse supports worker execution and streaming; use a preview of 100 rows, cap preview bytes, and stop sampling once the cap is reached. Preserve quoted multiline fields and parser errors. [20]
Label this local preview as provisional and keep upload and server-import progress separate. After the server validates the file and assigns stable row IDs, switch to the canonical dataset version. Do not enable full-dataset semantic execution against the sample or imply that the upload has finished. Preserve column widths and focus during the transition.
## Fetch a window of data
The browser API shares the MCP query service and permissions, but uses a larger transport envelope. Start with blocks of up to 256 rows and a 256 KiB uncompressed payload ceiling, requesting only visible and pinned columns. Fetch the first viewport first, then one or two nearby blocks in the scroll direction. Deduplicate requests; begin with two in flight and cancel obsolete work when the view changes.
Keep a byte-bounded least-recently-used cache, initially 32 MiB, rather than retaining every scrolled row. Shorten only display text, with an explicit truncation marker and a cell-detail endpoint for the original. Cap decoded strings and object overhead as well as wire bytes. A scroll jump loads its destination before speculative neighbors.
View request  dataset_version, result_revision, query_hash, generation  projected_columns, start, limit, max_bytesView response  view_id, view_revision, generation, rows, row_ordinals  total_count, count_status, next_start, has_more
A view is an ordered result snapshot. Materialize or cache its ordered row IDs once, then serve ordinal ranges without re-running a deep SQL OFFSET scan on every scroll. Include a deterministic row-ID tie breaker. Unknown counts remain labeled unknown; scrollbar extent and row positions become authoritative only for a completed view.
## Keep the grid and the data store separate
Maintain a display-index to source-ordinal mapping and a bounded cell cache outside a giant React row array. The grid reads cached values synchronously through getCellContent; onVisibleRegionChanged schedules block requests. Missing cells return lightweight placeholders. No network access, parsing, sorting, or full-table search belongs inside a render callback. [13, 25]
# 7  Fast filtering sorting and threshold changes
| Operation | Execution path | What the user sees |
| Threshold or category change | A worker scans complete cached score or label vectors and returns an index of matching rows. | The control responds immediately; counts and row order update without new inference. |
| Text filter or unavailable column | A server query runs against the frozen dataset version and returns the first result block. | The previous view remains visible with an Updating label until the new view is ready. |
| New semantic question | A durable inference job computes a new result column. | The column appears as pending immediately; committed answers arrive in batches. |
| Sort or group | Use a cached worker index for supported local keys; use the server for uncached keys, text collation, joins, and aggregates. | A consistent snapshot replaces the old view; selection follows stable row identity. |
## A compact cache makes the useful edits immediate
After initial paint, optionally fetch complete authorized score and label vectors for the current input scope. Ten Float64 score columns across 100,000 rows occupy 8 MB; across 300,000 rows they occupy 24 MB, before status arrays and metadata. A Uint32 row-order vector adds 0.4 MB or 1.2 MB. Keep these vectors in a worker with a separate 64 MiB budget and fall back to server execution when the full scope will not fit.
Use Float64 values and the same null, threshold, and tie-break rules as the server. Store source ordinals tied to an immutable version, plus completeness and status metadata. Cached viewport rows alone are never enough for a global filter. Partially computed score vectors yield a labeled partial view and denominator, not a complete dataset answer.
For a fixed score sort, precompute the sorted index once; threshold changes then select from that ordering. For several filters, combine masks. Reweighting can require a new sort and must stay off the main thread. Transfer result index buffers instead of cloning full row objects; transferred ArrayBuffers change ownership. Validate worker results against the server on shared edge-case fixtures. [21]
## Keep input responsive and prevent stale results
Echo keystrokes and slider motion immediately. Debounce network filters by about 150 ms; Enter applies immediately. Coalesce slider calculations and commit the final value on release or keyboard completion. Use an increasing generation ID plus cancellation so an older worker or server response can never replace a newer view. Slice long worker tasks so superseded work can yield.
Keep the previous result visible until the next viewport is ready, labeled with the applied filter. Commit rows, count, and filter state together. For a cache miss after a local index change, paint matching row identities with loading cells and fetch their projected values by ordinal. Save the canonical operation to the server for MCP, exports, and reproducibility; local speed does not change operation semantics.
# 8  Rendering and streaming without visual disruption
## Keep the paint path small
Use a fixed-height viewport with row and column virtualization. Start with 32 px rows and one or two pinned columns. Clamp long text to one line and open full content in the inspector. Avoid auto-sizing from the entire dataset, dynamic row heights during scrolling, and per-cell DOM widgets. Resize columns from explicit widths or a bounded sample.
Keep column definitions and getCellContent stable. Separate selection, toolbar state, job progress, and cell storage so typing in a command bar does not rebuild the grid. Cache expensive formatting and text measurements. Profile in a production build. AG Grid’s performance guide similarly identifies custom renderers as a source of scrolling overhead. [24]
Worker computation protects the main thread, but canvas drawing still consumes its frame budget. Budget about 8 ms of application work within a 16.7 ms frame at 60 Hz. Avoid long synchronous JSON parsing, row-array copies, and full-grid invalidation. React deferred rendering can prioritize typing, but does not move computation to a worker or debounce network calls. [22]
## Apply semantic results in bounded batches
Subscribe to sequenced job events through the web API. Events carry a result revision, progress, and changed row or column ranges; fetch projected values only for cached or visible cells. Coalesce data updates at roughly 100 ms intervals and paint dirty visible cells on the next animation frame. Glide exposes updateCells for targeted redraws. [14]
Sequence IDs allow replay after reconnecting; a gap triggers a snapshot refresh. If a tab falls behind, discard redundant intermediate invalidations and fetch the latest revision. Never drop an edit acknowledgement. Slow progress text to a few updates per second and pause rendering in background tabs; the server job continues.
## Preserve spatial and editing continuity
Keep source order stable while inference is streaming. In a ranked or filtered view, show an Updates available count and refresh explicitly or after a quiet interval. Do not move the active row while the user is scrolling, selecting, copying, or editing. Preserve the top visible row ID and pixel offset for patches; a deliberate new filter starts at the first result and announces the change.
Paint a user edit optimistically with a pending marker, then submit an idempotent versioned patch. On rejection, preserve the typed value in the editor and expose retry or undo. Mark dependent predictions stale. Keep selection by stable row ID, not displayed position; represent Select all as a view plus exclusions instead of enumerating the whole table.
Show loading only where data is missing. Keep the toolbar usable, reserve column widths before results arrive, delay spinners briefly to avoid flashes, and avoid animated row shuffling. Respect reduced motion. Bound clipboard work, for example to 10,000 cells or 1 MiB; route larger transfers through server export. Keep accessibility support enabled and test it with the real virtualized grid.
# 9  Frontend performance gates
These are proposed release budgets, not results already achieved. Test 100,000 and 300,000 rows with 30 columns, including ten semantic columns and long text; add a 100-column stress fixture. Run a one-million-row renderer fixture to expose scroll-range and cache problems without implying that the import or inference service supports that size.
| Measure | Initial acceptance target |
| Input and selection feedback | p95 under 50 ms from interaction to visible acknowledgement in the reference desktop run. |
| Scrolling | Target 60 Hz; at least 95% of measured active-scroll frame intervals at or below 20 ms on a 60 Hz display. No sustained blank viewport. |
| Cached numeric filter | p95 under 100 ms for the new count and row index at 100k rows; under 200 ms at 300k. Measure cell hydration separately. |
| Server filter | p95 under 500 ms from final input to first painted result block, including debounce, with warm data and 50 ms network RTT. |
| Cold imported view | p95 under 1 second from opening an already-imported dataset to a usable first viewport under the reference network profile. |
| New CSV preview | p95 under 500 ms after file selection for a bounded, normally formatted sample. Upload and full validation have separate timings. |
| Main-thread tasks | No application task over 50 ms during steady scrolling or typing; investigate longer tasks during initial parsing and decoding. |
| Memory | 32 MiB decoded-row cache plus 64 MiB vector budget; measure overhead and target under 200 MiB incremental tab memory for the base fixture. |
| Real-user responsiveness | Field INP at the 75th percentile at or below 200 ms. Measure scrolling separately because INP does not include scrolling. [23] |
## A reproducible performance test
Record the exact laptop, CPU, memory, browser version, viewport, refresh rate, and build hash. Use a midrange laptop profile, Chrome plus Firefox and Safari, 50 ms RTT and 20 Mbps downstream; repeat at 150 ms RTT and with CPU throttling. Record cold and warm runs separately. Gate the base profile; report degraded-profile results without implying identical latency.
Automate scroll flings, jumps to the last row, horizontal scrolling, filter typing, slider scrubbing, undo, editing, and selecting during a 1,000-cell-per-second update stream. Trace input, request, worker, cache, and paint timings. Track long tasks, dropped frames, memory growth, and stale-response errors. Performance acceptance must include accessibility and correct result counts.
Run this fixture before adding the full grid feature set, and keep it as a regression gate. If targets fail, reduce work per paint, packet size, or prefetch depth before increasing animation or hiding slow states behind an overlay.
# 10  Bulk execution design
## Planning a job
Validate the graph, freeze input versions, project only required fields, and apply exact filters before inference when doing so preserves the requested semantics. A filter before a global ranking or aggregate can change the answer; the compiler must preserve these boundaries. Estimate token volume, request count, candidate pairs, cache coverage, and dependencies before admitting a job.
Fuse independent questions about the same row into one Jev request where practical. A later question that needs an earlier answer is a separate graph stage. This distinction matters because answers within one provider request do not become inputs to other questions in that request. [3, 4]
## Batching is an experiment with a fallback
Begin with one row as shared state and several questions about it. To overcome request quotas, benchmark packing short rows into a request with explicit row references or question-local data. Keep each question tied to one row and account for the complete serialized prompt. Avoid large unrelated shared context, which the vendor identifies as a quality risk. [5]
Choose a token-aware packing strategy using both provider context limits, the number of questions, and measured quality. Test row isolation, reordered rows, long rows, and adversarial cell contents. If cross-row packing harms results, use smaller packets or one row per request and accept the associated throughput. The napkin math on page 15 assumes packing has passed this gate.
## Scheduling and caching
Use shared request and token rate limiters across all workers for each provider account. Bound in-flight requests and use fair queues so a large background job does not monopolize an interactive session. Retry transient errors with exponential backoff and jitter, obey provider retry signals, and checkpoint each committed chunk.
The cache key includes tenant, projected source content, semantic question, rubric, model, normalization, and prompt-layout versions. If packed neighbors influence the answer, the packet content and ordering must also participate in the key. Reuse row-level entries across repacking only after row isolation is validated. Threshold and sort changes reuse raw outputs without another model call.
## Candidate matching
Launch matching with a right-hand table of at most 5,000 rows, explicit comparison fields, and a capped candidate count such as five per left row. Generate candidates with exact blocking and lexical retrieval; add an embedding index only after measuring recall. Record the retrieval strategy and its version. Jev verifies the requested relationship against each candidate pair.
Validate candidate recall separately from the quality of pair judgments. One-to-one matching requires an explicit assignment step and conflict handling. For a 100,000-row left table, even five candidates creates 500,000 comparisons; the user must see that estimate before the job runs.
# 11  MCP tool surface
Expose the core system as a remote MCP server over Streamable HTTP using an official SDK that supports the chosen protocol revision. Pin and test supported revisions against actual clients. The current specification provides structured tool results and schemas; use application-level dataset, plan, and job handles for durable state. [7, 8]
All tools operate within the authenticated workspace. Mutating calls accept idempotency keys. Tool descriptions explain costs and partial-result behavior. The initial catalog is deliberately small enough to fit within an agent context.
| Tool | Contract |
| datasets_list | List accessible datasets with bounded pagination and brief metadata. |
| datasets_describe | Return schema, row count, versions, quality statistics, and an optional bounded sample. |
| uploads_prepare | Issue a short-lived upload target and upload ID for direct byte transfer by a client or runtime. |
| datasets_import | Import a completed upload ID with explicit parse options; return an import job handle. |
| datasets_patch | Apply bounded row corrections or overrides and return a new version without changing the source snapshot. |
| plans_compile | Optional language-to-plan conversion using bounded context and an explicit planning budget. |
| plans_validate | Validate a typed plan and return its hash, work estimate, warnings, and required scopes. No paid inference by default. |
| jobs_submit | Run a validated plan against frozen versions with budgets, a deadline, and an idempotency key. |
| jobs_get | Return state, progress, usage, result handles, and a bounded error summary. Suggest a next polling interval. |
| jobs_cancel | Stop scheduling further work and finalize committed partial results. In-flight requests may still incur cost. |
| results_query | Project, filter, sort, inspect selected rows, or aggregate a result through a constrained expression tree. |
| results_export | Create a CSV or Parquet artifact with a resource or download handle and a completeness manifest. |
| datasets_delete | Delete an authorized dataset and its dependent retained objects under the published retention policy. |
The host uploads files directly; a path on an agent’s machine is not a file the remote MCP server can read. Raw CSV must not be serialized into tool arguments. Export artifacts likewise travel through a download path outside the model context. The tool returns a handle, size, format, and status.
# 12  MCP contracts and context budgets
## A bounded conversation with a large dataset
A frontier model describes the dataset, validates a bulk plan, submits one job, and asks for a small aggregate or selected exceptions. It does not loop over rows. The server does not ask the client model to classify rows through MCP sampling, and no worker silently falls back to the caller’s frontier model.
The spreadsheet and MCP share all core capabilities through plans and result versions. The UI may show additional presentation controls, but any import, semantic operation, correction, query, cancellation, or export must have a corresponding MCP route. Callers that already supply a typed plan skip plans_compile entirely.
| Boundary | Proposed default |
| Schema and samples | Schema summaries up to 100 columns; sample off unless requested, then at most 20 rows and 8 KiB. Paginate wider schemas. |
| Query results | At most 50 rows and 16 KiB per response, including text and structured data. Apply a lower caller-specified output budget when supplied. |
| Long cell text | Return an explicit truncated flag and a row or cell handle. A narrow follow-up query can retrieve a bounded slice. |
| Job progress | Compact counts and handles only. Default polling interval 2 seconds, backing off to 5–10 seconds for long jobs. |
| Full data | Files and server-side result handles. Never automatically expand a resource into the tool result. |
## Response envelope
Declare inputSchema and outputSchema for each tool. Return a stable structured envelope with request_id, data, warnings, and next_cursor where applicable. Dataset pages include schema plus bounded rows. Use a short text summary for compatibility rather than duplicating the full JSON in prose. Mark truncation, approximation, and partial execution explicitly. [8]
A job result identifies its source version and operation scope. Track succeeded, failed, pending, and skipped source rows as disjoint counts; uncertainty is a property of successful predictions, and cache hits are a subset of successes. Report inference attempts and pair counts separately so operators with several judgments do not inflate the source-row denominator.
## Error and lifecycle behavior
Validation errors return a field path, error code, and actionable correction. Execution failures distinguish retryable provider errors, invalid data, insufficient permissions, and exhausted budgets. A client can inspect a durable job after reconnecting. Closing an MCP request does not cancel a persisted job; jobs_cancel does.
Retrying jobs_submit with the same tenant, idempotency key, and payload returns the same job. Reusing the key with a different payload fails. A new job can reuse completed predictions, but that is distinct from retrying the same submission. A completed output is immutable; later corrections create a new result version.
# 13  Example agent workflow
Task: “Find onboarding feedback, rank its severity, and export the strongest matches.” The agent receives a dataset handle from a direct upload, reads its schema, and submits a typed plan. The example is application payload, not the full MCP wire envelope.
{  "plan_version": "1",  "source": {"dataset_id": "feedback", "version_id": "v1"},  "model": "jev-1.13.0",  "steps": [    {"id": "labels", "op": "semantic_annotate", "input": "source",     "columns": ["feedback_text"],     "questions": [       {"name": "onboarding", "kind": "boolean",        "instruction": "Does the text describe an onboarding problem?",        "thresholds": {"true_min": 0.85, "false_max": 0.15}},       {"name": "severity", "kind": "score",        "instruction": "Rate the impact described in the text.",        "levels": ["No stated disruption", "Work slowed",                   "Core workflow blocked", "Service unusable"]}     ], "on_missing": "unknown"},    {"id": "selected", "op": "filter", "input": "labels",     "where": {"column": "onboarding.value",               "operator": "eq", "value": true},     "unknown_policy": "separate"},    {"id": "ranked", "op": "sort", "input": "selected",     "by": [{"column": "severity.score", "direction": "desc"},            {"column": "_row_id", "direction": "asc"}]}  ],  "output": "ranked"}
The illustrative thresholds produce true, false, or null for uncertainty; status distinguishes uncertainty from missing input. Raw probabilities remain stored. These thresholds are not accuracy guarantees. The adapter maps logical questions to the provider schema and pins the model version; recheck availability during implementation.
## Submission and result handling
plans_validate returns plan_hash and estimates without making inference calls. jobs_submit accepts that hash, an idempotency key, and limits such as max_source_rows = 10000, max_provider_requests = 12000, spend_target_usd = 0.50, and deadline_seconds = 120. The large request cap allows an unpacked fallback; the deadline may stop that fallback early.
jobs_get returns a durable job ID, state, source counts, provider usage, and result_version_id. The agent can call results_query for the top 20 rows or an aggregate, then results_export for the full result. Records outside the filter and uncertain records remain available through the labeled intermediate result and review view.
For “accounts affected,” add an exact projection of distinct account IDs and a key join to the accounts table before aggregating revenue. This step runs over stored data without further semantic inference.
# 14  Reliability permissions and data handling
## Job and budget behavior
Persist state transitions: queued, running, then succeeded, partial, failed, or cancelled. Record a terminal reason such as deadline, budget, or provider failure. “Succeeded” means every required input was processed successfully; an unknown semantic answer can still be a successfully computed result. Missing chunks and failed rows prevent a claim of complete coverage.
Commit chunks transactionally with a unique job, stage, and chunk key so retries cannot duplicate rows. A provider timeout may have been billed even when no response arrives; do not promise exactly-once billing. Track ambiguous attempts, bound retries, and reuse any committed results after worker restarts.
Enforce hard caps on source rows, comparisons, provider requests, and returned bytes. Reserve a conservative cost estimate before dispatch and account for all in-flight work. Reconcile provider usage after each response. A dollar target is an admission and scheduling guard; absent a provider-enforced cap, invoice-level precision cannot be guaranteed. Stop dispatch when estimated spend plus reservations would exceed the target.
## Workspace isolation
Use MCP HTTP authorization and audience validation through the supported SDK and authorization service. Check workspace access and object permissions on every call; possession of an opaque dataset ID grants no access. Separate read, run, import, export, and delete scopes. Keep provider keys on the server and isolate cached data by tenant. [9]
Dataset deletion stops dependent work, removes active data and caches, and invalidates download links. Publish a retention period for demo uploads and a separate backup deletion window. A proposed demo default is seven days, shown at upload. Avoid raw cell values in general application logs; retain only authorized provenance and deliberately collected evaluation data.
## Untrusted table contents
Treat every cell as data, including text that resembles an instruction. Delimit source content, keep it separate from the question, and test whether malicious rows can alter other rows’ labels. Provider output type constraints do not establish factual correctness or prevent all prompt injection. The model never chooses tools, credentials, or access scopes. [5]
Accept uploads through server-issued upload targets. Defer arbitrary remote URL ingestion; adding it later requires protection against internal-network requests and redirect abuse. Enforce file-size, row-count, parser-time, and decompression limits. The exact query layer exposes only approved operations and cannot load extensions or reach arbitrary files.
CSV export should escape formula-leading text for spreadsheet consumers by default, with a clearly labeled raw export option. A sidecar manifest records model and plan versions, completed scope, exclusions, errors, and any approximation in matching or ranking.
# 15  Cost and latency model
The published Jev model page lists jev-1.13.0 at $0.042 per million input tokens, free output tokens, 250,000 tokens per second, and 1,200 requests per minute. It lists a 64k total-input limit and a 32k state-plus-longest-question limit. Quotas can change and are shared capacity constraints, not measured application throughput. [4]
## An illustrative short row workload
Assume one semantic pass averages 500 billed input tokens per row, including instructions and serialization, and an evaluated packing strategy fits 20 rows per request. Model the request quota as a steady 20 requests per second without bursts, with exclusive quota access and no cache hits. These are planning assumptions, not benchmark results.
| Rows | Input tokens | Jev cost | Quota floor |
| 1,000 | 0.5 million | $0.021 | 2.5 seconds |
| 10,000 | 5 million | $0.21 | 25 seconds |
| 100,000 | 50 million | $2.10 | 4 minutes 10 seconds |
| 300,000 | 150 million | $6.30 | 12 minutes 30 seconds |
Cost = billed input tokens ÷ 1,000,000 × $0.042. For a single stage, a useful lower bound on processing time is the maximum of token volume ÷ token quota, request count ÷ request quota, and request count × mean request latency ÷ concurrency. Dependent stages, startup, retries, contention, and storage add time.
The request quota is decisive: at one row per request, 100,000 rows require at least 5,000 seconds, or about 83 minutes, before other overhead. At 20 rows per request, the request floor falls to 250 seconds; the token floor is 200 seconds. Packing quality and actual account quotas are therefore launch-critical measurements.
## What MCP saves
At 100,000 rows and 500 tokens per row, a naïve frontier scan would expose roughly 50 million tokens of table work to the frontier model. This design can target under 10,000 frontier-visible tokens for schema, plan, progress, and an aggregate answer. That is roughly 5,000 times less context volume in this example, not a measured cost or accuracy comparison.
The workload still incurs Jev inference, planning where requested, storage, retrieval, and execution costs. Several semantic passes or candidate-pair evaluation increase the total. Fuse independent columns to reuse input state, but measure complete serialized tokens rather than assuming each new column is free.
## Proposed performance gates
For a warm service and reserved quota, target a first committed chunk within three seconds after job admission and completion of the illustrative 10,000-row workload within 45 seconds. Measure p50 and p95 separately. Validate these targets before using “instant” language; 100,000-row scans are background jobs.
# 16  Validation and delivery plan
## Measure usefulness and accuracy together
Prepare labeled feedback, agent-evaluation, and catalog datasets with short and long text, missing values, ambiguous examples, and prompt-injection attempts. Use a held-out set for reported quality. Compare Jev with a frontier baseline on the same inputs and rubrics; use human labels to resolve disagreements rather than treating the baseline as ground truth.
Measure classification precision and recall, score agreement, ranking usefulness, match-candidate recall, accepted-match precision, and abstention coverage. For a starter template, a provisional gate is at least 95% precision on automatically accepted decisions with at least 80% coverage, using a large enough labeled set to report uncertainty. This is a template gate, not a guarantee for arbitrary user questions.
Benchmark packet sizes 1, 5, 10, and 20. Report row throughput, input tokens per row, end-to-end latency, provider errors, and quality change. A proposed packing gate is no more than one percentage point loss in the relevant quality metric versus isolated rows, supported by confidence intervals. Favor a smaller packet if evidence is inconclusive.
| Milestone | Exit condition |
| 1  Engine and MCP | Import → validate plan → run Jev → inspect bounded results → export works without the spreadsheet. Retries, cancellation, versioning, and spend accounting survive a worker restart. |
| 2  Spreadsheet demo | Upload, semantic columns, filtering, ranking, corrections, undo, and export work. The frontend gates on page 9 pass, and the same plan yields the same stored result through MCP. |
| 3  Matching and scale | Candidate recall, 10,000-row latency, and a 100,000-row background run are measured. Raise the row cap to 300,000 only after frontend, backend, budget, and isolation tests pass. |
| 4  Wider ingestion | Add XLSX values and Parquet input after the core contracts are stable. Expand provider quotas only against measured workload demand. |
## Launch acceptance
Prove that threshold, weight, and sort edits make zero provider calls when inputs are unchanged. Verify that partial results are never labeled complete, retries never duplicate committed rows, and unauthorized handles cannot expose another workspace’s data. Confirm export counts and exact aggregates against deterministic reference calculations.
Run an MCP acceptance scenario with 100,000 rows: one submitted bulk plan, bounded result retrieval, no raw-table tool output, and no server-initiated frontier inference. Track serialized response bytes and estimate context tokens with the target client tokenizer. Report actual frontier cost only when the client exposes its billing usage.
# 17  Open decisions and references
## Decisions to resolve with the first benchmark
The largest uncertainty is whether multi-row packing preserves useful accuracy for each supported task. That result determines realistic throughput at the default request quota. Next, test whether lexical candidate generation is sufficient for catalog matching or whether an embedding index is necessary.
Choose the first supported MCP clients and verify their protocol and authorization behavior before locking the SDK version. Decide whether the public demo uses a shared provider account or per-workspace credentials, since shared quotas directly affect latency. Set retention and spend defaults from the intended demo environment.
## Primary references
Vendor and protocol documentation checked on 21 September 2026. Provider prices, limits, model aliases, and protocol support should be rechecked during implementation. The architecture, budgets, quality gates, and user experience in this document are proposed design choices.
[1] LOTUS project and semantic operator examples
[2] LOTUS semantic top k documentation
[3] TypeSafe Jev primitives
[4] TypeSafe models and published limits
[5] Jev 1 13 model jaggedness and known limitations
[6] TypeSafe API reference
[7] MCP transport specification 2026 07 28
[8] MCP tool specification 2026 07 28
[9] MCP authorization specification 2026 07 28
[10] LOTUS semantic join documentation
[11] LOTUS semantic aggregation documentation
## Implementation starting point
Build the headless import-to-export path first and use MCP as its first external client. This forces plans, budgets, result handles, and lifecycle behavior to be complete before the grid is added. The spreadsheet then becomes a visual editor and inspector for the same durable computation.
# 18  Frontend research references
Primary implementation documentation checked on 21 September 2026. Large-row examples and vendor claims informed the design; no library benchmark was run as part of this document update. Pin dependency versions and rerun the application fixture before selecting the final grid.
[12] Glide Data Grid architecture features and license
[13] Glide DataEditor API and data callbacks
[14] Glide targeted cell updates
[15] AG Grid row and column virtualization
[16] AG Grid Server Side Row Model
[17] AG Grid Community and Enterprise feature comparison
[18] TanStack Table filtering and large row counts
[19] TanStack Table virtualization integration
[20] Papa Parse worker and streaming configuration
[21] Web Workers and transferable buffers
[22] React deferred rendering and its limits
[23] Interaction to Next Paint measurement
[24] AG Grid scrolling performance guidance
[25] Glide viewport notifications and sizing