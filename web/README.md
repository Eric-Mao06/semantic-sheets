# Semantic Sheet — web app

React 18 + TypeScript + Vite. Styling is Tailwind CSS v4 with a small set of shadcn-style primitives in
`src/components/ui/` (Radix under the hood), so anything published in the shadcn registry format drops straight in:

```bash
npx shadcn@latest add <component>
```

`components.json` carries the registry aliases (`@/components/ui`, `@/lib/utils`) and points the CSS at
`src/styles.css`, where the design tokens live.

## Scripts

```bash
npm run dev         # http://localhost:5173, proxies /api and /mcp to :8000
npm run typecheck   # tsc -p tsconfig.app.json --noEmit
npm run lint        # oxlint
npm run build       # tsc -b && vite build → dist/ (served by the FastAPI process in the Docker image)
```

`VITE_API_BASE` (default empty) points the app at an API on another origin.

## Structure

```
src/App.tsx               landing ↔ workbench switch, #/d/<dataset_id> deep links
src/api.ts                typed fetch wrapper, bearer token, SSE job events with replay
src/types.ts              API response types (mirrors server/semsheet/models.py where they overlap)
src/components/
  Landing.tsx             sample datasets, drag-and-drop upload with a client-side preview, recent datasets
  Workbench.tsx           grid + command bar + panels; owns the current result version and job
  OperationPanel.tsx      plain-language summary of the plan, estimate, Run button
  PlanEditor.tsx          full step editor and job limits behind "Details & edit"
  Inspector.tsx           per-cell provenance (raw Jev answer, score, status, overrides)
  History.tsx             jobs and result versions for the dataset
  QuickFilter.tsx         local filter/sort over result vectors
  Grid.tsx                Glide Data Grid binding
  ui/                     button, input, badge, card, collapsible, tabs, tooltip, dialog, dropdown-menu,
                          switch, slider, progress, toast, misc (label, spinner, stat…)
src/lib/describe.ts       plan → sentences ("Read “text” for every row and answer this question…")
src/lib/utils.ts          cn(), money / duration / count formatting, job-state wording
src/data/ViewController   paged, cached result views for the grid
src/worker/               local filter / sort over result vectors (Web Worker)
src/hooks/useMediaQuery   responsive breakpoints
```

## Design

Paper white, near-black ink, hairline rules, one blue. Labels are small mono caps (`label-mono`). Every surface
carries a faint film grain: a page-wide fixed layer (`body::after`) plus the `grain` / `grain-dark` utilities on
cards, notices and inverted buttons. Fonts ship with the bundle (`@fontsource`): Geist, Geist Mono and
Instrument Serif for display headings.

## Dependencies worth knowing about

`lodash`, `marked` and `react-responsive-carousel` are peer dependencies of `@glideapps/glide-data-grid` and are
not used directly. `papaparse` parses the first 512 KiB of an upload in a worker for the provisional preview; the
canonical import always happens on the server.
