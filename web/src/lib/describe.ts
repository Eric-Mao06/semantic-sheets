import type { Expr, Plan, Question, Step } from "@/types";

/**
 * Plain-language rendering of a plan: one short sentence per step, in execution order, written for
 * someone who has never seen the step JSON. This is what the Operation panel shows by default.
 */

export type StepSentence = { id: string; text: string; produces?: string[] };

const OP_WORDS: Record<string, string> = {
  eq: "is",
  ne: "is not",
  gt: "is more than",
  gte: "is at least",
  lt: "is less than",
  lte: "is at most",
  contains: "contains",
  icontains: "contains",
  starts_with: "starts with",
  is_null: "is empty",
  not_null: "is filled in",
  in: "is one of",
  not_in: "is none of",
  like: "looks like",
  regex: "matches the pattern",
};

/** `severity.value` → `severity`; `_row_id` → `row number`; the rest verbatim in quotes. */
export function humanColumn(name: string): string {
  if (name === "_row_id") return "the row number";
  const base = name.replace(/\.(value|score|confidence|status|near|raw|right_row_id|candidates)$/, (_, suffix: string) => (suffix === "value" ? "" : ` ${suffix}`));
  return `“${base}”`;
}

function humanLiteral(v: unknown): string {
  if (v === null || v === undefined) return "empty";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return v.toLocaleString("en-US");
  if (Array.isArray(v)) return v.map(humanLiteral).join(", ");
  return `“${String(v)}”`;
}

export function humanWhere(e: Expr, depth = 0): string {
  if ("column" in e) return humanColumn(e.column);
  if ("literal" in e) return humanLiteral(e.literal);
  const op = e.op;
  const args = e.args ?? [];
  if (op === "and" || op === "or") {
    const parts = args.map((a) => humanWhere(a, depth + 1));
    const joined = parts.join(op === "and" ? " and " : " or ");
    return depth > 0 ? `(${joined})` : joined;
  }
  if (op === "not") return `not (${humanWhere(args[0], depth + 1)})`;
  if (op === "is_null" || op === "not_null") return `${humanWhere(args[0], depth + 1)} ${OP_WORDS[op]}`;
  const word = OP_WORDS[op] ?? op;
  const [a, b] = args;
  if (a && b && "column" in a && "literal" in b && a.column.endsWith(".value") && typeof b.literal === "boolean") {
    return `${humanColumn(a.column)} is ${b.literal === (op !== "ne") ? "yes" : "no"}`;
  }
  return `${a ? humanWhere(a, depth + 1) : "?"} ${word} ${b ? humanWhere(b, depth + 1) : ""}`.trim();
}

function list(items: string[], conj = "and"): string {
  if (items.length <= 1) return items[0] ?? "";
  if (items.length === 2) return `${items[0]} ${conj} ${items[1]}`;
  return `${items.slice(0, -1).join(", ")} ${conj} ${items[items.length - 1]}`;
}

function describeQuestion(q: Question): string {
  const instr = q.instruction.trim().replace(/\s+/g, " ");
  if (q.kind === "boolean") return `decide yes or no: ${instr}`;
  if (q.kind === "category") {
    const labels = Object.keys(q.options ?? {});
    return `sort each row into one of ${labels.length} categories (${list(labels.map((l) => `“${l}”`), "or")}) — ${instr}`;
  }
  const lv = q.levels ?? [];
  return lv.length >= 2 ? `rate each row from “${lv[0]}” to “${lv[lv.length - 1]}” — ${instr}` : `score each row — ${instr}`;
}

/** Steps that lead to the output, in execution order; falls back to plan order if the chain is broken. */
export function orderedSteps(plan: Plan): Step[] {
  const byId = new Map(plan.steps.map((s) => [s.id, s]));
  const chain: Step[] = [];
  let cur: Step | undefined = byId.get(plan.output);
  const seen = new Set<string>();
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id);
    chain.unshift(cur);
    cur = byId.get(cur.input);
  }
  return chain.length ? chain : plan.steps;
}

export function describeStep(s: Step, isOutput: boolean): StepSentence {
  switch (s.op) {
    case "semantic_annotate": {
      const cols = list(s.columns.map(humanColumn));
      const qs = s.questions.map(describeQuestion);
      const intro = `Read ${cols} for every row and ${qs.length === 1 ? "" : `answer ${qs.length} questions: `}`;
      return { id: s.id, text: `${intro}${qs.length === 1 ? qs[0] : qs.map((q, i) => `(${i + 1}) ${q}`).join("; ")}.`, produces: s.questions.map((q) => q.name) };
    }
    case "filter":
      return { id: s.id, text: `Keep only the rows where ${humanWhere(s.where)}.${s.unknown_policy === "exclude" ? " Rows the model was unsure about are left out." : s.unknown_policy === "include" ? "" : " Rows the model was unsure about are set aside for review."}` };
    case "sort":
      return { id: s.id, text: `Sort by ${list(s.by.map((k) => `${humanColumn(k.column)} (${k.direction === "desc" ? "highest first" : "lowest first"})`))}.` };
    case "aggregate": {
      const metrics = s.metrics.map((m) => (m.fn === "count" && !m.column ? "count the rows" : `${m.fn === "count_distinct" ? "count the distinct values of" : m.fn === "avg" ? "average" : m.fn} ${m.column ? humanColumn(m.column) : ""}`.trim()));
      return { id: s.id, text: s.group_by.length ? `For each ${list(s.group_by.map(humanColumn))}, ${list(metrics)}.` : `Across all rows, ${list(metrics)}.`, produces: s.metrics.map((m) => m.name) };
    }
    case "project":
      return { id: s.id, text: `Show only the columns ${list(s.columns.map(humanColumn))}.` };
    case "compute":
      return { id: s.id, text: `Add ${s.columns.length === 1 ? "a calculated column" : `${s.columns.length} calculated columns`} ${list(s.columns.map((c) => `“${c.name}”`))}.`, produces: s.columns.map((c) => c.name) };
    case "join":
      return { id: s.id, text: `Bring in columns from another table where ${list(s.on.map((k) => `${humanColumn(k.left)} matches ${humanColumn(k.right)}`))}${s.how === "left" ? ", keeping rows with no match" : ""}.` };
    case "semantic_match":
      return { id: s.id, text: `Find the best matching row in another table for each row, judged by: ${s.instruction.trim()}.`, produces: [s.name ?? "match"] };
    case "distinct":
      return { id: s.id, text: s.columns?.length ? `Remove rows that repeat the same ${list(s.columns.map(humanColumn))}.` : "Remove duplicate rows." };
    case "limit":
      return { id: s.id, text: `Show the first ${s.n.toLocaleString("en-US")} rows${isOutput ? "" : " only"}.` };
    default:
      return { id: (s as Step).id, text: `Run the “${(s as Step).op}” step.` };
  }
}

export function describePlan(plan: Plan): StepSentence[] {
  return orderedSteps(plan).map((s) => describeStep(s, s.id === plan.output));
}

/** Column names the operation adds to the table (what the user will see appear). */
export function newColumns(plan: Plan): string[] {
  const out: string[] = [];
  for (const s of orderedSteps(plan)) {
    const d = describeStep(s, false);
    if (d.produces) out.push(...d.produces);
  }
  return out;
}

export function opLabel(op: string): string {
  return (
    {
      semantic_annotate: "Judge with the model",
      semantic_match: "Match with the model",
      filter: "Filter",
      sort: "Sort",
      aggregate: "Group & count",
      project: "Choose columns",
      compute: "Calculate",
      join: "Join",
      distinct: "Deduplicate",
      limit: "Limit",
    }[op] ?? op
  );
}
