# Semantic Sheet — web

React 18 + TypeScript + Vite. Styling is Tailwind CSS v4 with a small set of shadcn-style primitives in
`src/components/ui/` (Radix under the hood), so anything published in the shadcn registry format — including
components from [21st.dev](https://21st.dev) — drops straight in:

```bash
npx shadcn@latest add "https://21st.dev/r/<author>/<component>"
```

`components.json` carries the registry aliases (`@/components/ui`, `@/lib/utils`) and points the CSS at
`src/styles.css`, where the design tokens live.

## Design

Paper white, near-black ink, hairline rules, one blue. Labels are small mono caps (`label-mono`). Every surface
carries a faint film grain: a page-wide fixed layer (`body::after`) plus the `grain` / `grain-dark` utilities on
cards, notices and inverted buttons. Fonts ship with the bundle (`@fontsource`): Geist, Geist Mono and
Instrument Serif for display headings.

## Structure

```
src/components/ui/        button, input/textarea/native-select, badge, card, collapsible, tabs, tooltip,
                          dialog, dropdown-menu, switch, slider, progress, toast, misc (label, spinner, stat…)
src/components/           Landing, Workbench, OperationPanel (plain-language summary + Run),
                          PlanEditor (full step editor behind “Details & edit”), Inspector, History,
                          QuickFilter, Grid (Glide Data Grid)
src/lib/describe.ts       plan → sentences (“Read “text” for every row and answer this question…”)
src/lib/utils.ts          cn(), money / duration / count formatting, job-state wording
src/data/ViewController   paged, cached result views for the grid
src/worker/               local filter / sort over result vectors
```

## Scripts

```bash
npm run dev       # http://localhost:5173, proxies /api and /mcp to :8000
npm run build     # tsc -b && vite build → dist/
npm run lint      # oxlint
```
