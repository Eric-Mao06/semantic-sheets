/**
 * Computer-use walkthrough of Semantic Sheets: drives the real product in Chromium, records a video and
 * numbered screenshots with captions into ./walkthrough (or $WALKTHROUGH_DIR).
 *
 * Prerequisites: API running on http://127.0.0.1:8000 with the sample datasets prepared and a workspace
 * key in $SS_DEV_WORKSPACE_KEY (default dev-key). Uses the real planner and Jev unless the server runs
 * with SS_FAKE_JEV=1.
 */
import { chromium } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const OUT = process.env.WALKTHROUGH_DIR || path.resolve("walkthrough");
const BASE = process.env.SS_BASE_URL || "http://127.0.0.1:8000";
const KEY = process.env.SS_DEV_WORKSPACE_KEY || "dev-key";
const EXEC = process.env.CHROMIUM_PATH || (fs.existsSync("/opt/pw-browsers/chromium") ? "/opt/pw-browsers/chromium" : undefined);
const SAMPLE_CSV = process.env.WALKTHROUGH_UPLOAD || "";
fs.mkdirSync(OUT, { recursive: true });
const steps = [];
let n = 0;

const browser = await chromium.launch({ executablePath: EXEC, slowMo: 60 });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, recordVideo: { dir: OUT, size: { width: 1440, height: 900 } } });
const page = await context.newPage();
page.on("pageerror", (e) => console.log("pageerror:", e.message));

async function shot(caption, opts = {}) {
  n += 1;
  const file = `${String(n).padStart(2, "0")}_${caption.toLowerCase().replace(/[^a-z0-9]+/g, "_").slice(0, 40)}.png`;
  await page.waitForTimeout(opts.wait ?? 400);
  await page.screenshot({ path: path.join(OUT, file) });
  steps.push({ n, file, caption, at: new Date().toISOString() });
  console.log(`[${n}] ${caption}`);
}
async function status() { return (await page.textContent("[data-testid=status-strip]")) || ""; }
async function clickCell(x, y) {
  // Glide draws cells on a canvas behind a scroller overlay: use raw mouse coordinates.
  const box = await page.locator("[data-testid=data-grid-canvas]").first().boundingBox();
  await page.mouse.click(box.x + x, box.y + y);
}
async function waitJob(timeoutMs = 600000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const s = await page.textContent("[data-testid=job-state]").catch(() => "");
    if (s && !/queued|running/.test(s)) return s;
    await page.waitForTimeout(500);
  }
  throw new Error("job did not finish");
}

// 1. Landing
await page.goto(`${BASE}/`);
await page.fill("input[placeholder='workspace key']", KEY);
await page.click("text=Open workspace");
await page.waitForSelector("[data-testid=dropzone]");
await shot("Landing: upload control, sample datasets, workspace budget");

// 2. Upload a CSV through the drop zone (local preview + server import)
if (SAMPLE_CSV) {
  await page.setInputFiles("[data-testid=dropzone] input[type=file]", SAMPLE_CSV);
  await page.waitForSelector("[data-testid=import-start]:not([disabled])", { timeout: 120000 });
  await shot("Import dialog: provisional local preview, server sniff, parse options", { wait: 800 });
  await page.click("[data-testid=import-start]");
  await page.waitForSelector("[data-testid=dataset-name]", { timeout: 300000 });
} else {
  await page.click("[data-testid=sample-bitext_support]");
  await page.waitForSelector("[data-testid=dataset-name]", { timeout: 120000 });
}
await page.waitForTimeout(1500);
await shot("Sheet: original table with detected types, row count, command bar");

// 3. Plan from plain language (gpt-6-astra, bounded schema + 20 sample rows)
const REQUEST = process.env.WALKTHROUGH_REQUEST || "Find customers trying to cancel an order because they cannot afford it, classify the intent of every request, rate how urgent each one is, and show the affordability cancellations most urgent first";
await page.fill("[data-testid=command-input]", REQUEST);
await page.click("[data-testid=command-plan]");
await shot("Planner running: the model sees schema + sample only", { wait: 1200 });
await page.waitForSelector("[data-testid=operation-panel] .step", { timeout: 400000 });
await shot("Editable operation steps with estimate, cost and warnings");
await page.locator("[data-testid=operation-panel]").evaluate((el) => { el.scrollTop = el.scrollHeight; });
await shot("Operation panel: filter, sort, estimate and spend target");

// 4. Run: streaming chunks, status strip, provisional ranking
await page.click("[data-testid=plan-run]");
await page.waitForSelector("[data-testid=job-state]", { timeout: 60000 });
await page.waitForTimeout(2500);
await shot("Job running: completed chunks appear, status strip shows examined/remaining/spend");
const finalState = await waitJob();
await page.waitForTimeout(1500);
await shot(`Job ${finalState}: ranked result with semantic columns`);
console.log("status:", await status());

// 5. Inspect a row: raw probabilities and provenance
await clickCell(260, 60);
await page.waitForSelector("[data-testid=inspector]", { timeout: 20000 });
await shot("Inspector: row values, raw model distribution, corrections");

// 6. Threshold edit: local count, then re-run with zero provider calls
await page.click("[data-testid=tab-plan]");
const slider = page.locator("[data-testid^=slider-][data-testid$=-true]").first();
if (await slider.count()) {
  await slider.focus();
  for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowLeft");
  await page.waitForTimeout(600);
  await shot("Threshold lowered: instant local count over cached probabilities");
  await page.click("[data-testid=plan-run]");
  await page.waitForSelector("[data-testid=job-state]", { timeout: 60000 });
  await waitJob();
  await page.waitForTimeout(1200);
  await shot("Re-run after threshold edit: 0 provider requests, all cache hits");
  console.log("status:", await status());
}

// 7. Review view: unknown rows of the filter
const review = page.locator("[data-testid^=review-]").first();
if (await review.count()) {
  await review.click();
  await page.waitForTimeout(1500);
  await shot("Review view: rows whose filter answer is unknown or uncertain");
  await review.click();
  await page.waitForTimeout(800);
}

// 8. Correction -> new result version; Versions tab = undo
await clickCell(260, 60);
await page.waitForSelector("[data-testid=inspector]", { timeout: 20000 });
const target = page.locator("[data-testid^=cell-][data-testid$='.value']").first();
if (await target.count()) {
  await target.dblclick();
  await page.fill("[data-testid=inspector-edit-input]", "other");
  await page.click("[data-testid=inspector-edit-save]");
  await page.waitForSelector("[data-testid=notice]", { timeout: 20000 });
  await shot("Correction saved as a new result version; raw output untouched");
  await page.click("[data-testid=tab-versions]");
  await page.waitForTimeout(600);
  await shot("Versions: results, jobs and dataset versions (undo = select an earlier one)");
}

// 9. Export
await page.click("[data-testid=export-csv]");
await page.waitForSelector("[data-testid=export-banner]", { timeout: 60000 });
await shot("Export: CSV artifact with completeness manifest");

fs.writeFileSync(path.join(OUT, "steps.json"), JSON.stringify({ base: BASE, request: REQUEST, steps }, null, 2));
await context.close();
await browser.close();
const vids = fs.readdirSync(OUT).filter((f) => f.endsWith(".webm"));
if (vids.length) fs.renameSync(path.join(OUT, vids[0]), path.join(OUT, "walkthrough.webm"));
console.log("done", OUT);
