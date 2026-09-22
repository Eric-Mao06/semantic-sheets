import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DataEditor, GridCellKind, type DataEditorRef, type EditableGridCell, type GridCell, type GridColumn, type Item, type Rectangle, type Theme } from "@glideapps/glide-data-grid";
import type { ViewController } from "../data/ViewController";
import type { ColumnInfo } from "../types";

type Props = {
  controller: ViewController;
  columns: ColumnInfo[];
  dataVersion: number;
  editable: Set<string>;
  pendingEdits: Map<string, unknown>;
  /** Touch layout: taller rows, no row-marker gutter or frozen column, narrower text columns. */
  compact?: boolean;
  onCellClick: (displayIndex: number, column: ColumnInfo) => void;
  onEdit: (rowId: number, column: string, value: unknown) => void;
};

// Palette mirrors the CSS tokens in styles.css (Glide paints on canvas, so it cannot read CSS variables).
const INK = "#201f23";
const MUTED = "#767676";
const TERTIARY = "#a2a2a2";
const OK = "#2f8f1f";
const WARN = "#b8791a";
const BAD = "#d13b3b";
const LINK = "#3030d7";

const STATUS_THEME: Record<string, Partial<Theme>> = {
  ok: { textDark: OK, bgCell: "#f1f8ee" },
  override: { textDark: LINK, bgCell: "#eeeefb" },
  uncertain: { textDark: WARN, bgCell: "#f8f0e1" },
  missing: { textDark: MUTED, bgCell: "#f3f3f5" },
  pending: { textDark: TERTIARY, bgCell: "#fafafa" },
  failed: { textDark: BAD, bgCell: "#fbf2f6" },
  input_too_long: { textDark: BAD, bgCell: "#fbf2f6" },
  matched: { textDark: OK, bgCell: "#f1f8ee" },
  unmatched: { textDark: MUTED, bgCell: "#f3f3f5" },
  no_candidates: { textDark: MUTED, bgCell: "#f3f3f5" },
  skipped: { textDark: TERTIARY, bgCell: "#fafafa" },
};

function widthFor(c: ColumnInfo, compact: boolean): number {
  const header = Math.min(compact ? 200 : 260, 24 + c.name.length * 7.2); // wide enough to read the header
  if (c.name === "_row_id") return compact ? 60 : 70;
  if (c.name.endsWith(".status")) return Math.max(96, header);
  if (c.name.endsWith(".score") || c.name.endsWith(".confidence")) return Math.max(92, header);
  if (c.type === "integer" || c.type === "double") return Math.max(compact ? 96 : 110, header);
  if (c.type === "boolean") return Math.max(90, header);
  if (c.type === "date" || c.type === "timestamp") return Math.max(120, header);
  if (c.role === "semantic" || c.name.endsWith(".value")) return Math.max(compact ? 150 : 170, header);
  return compact ? 220 : 300;
}

export default function Grid({ controller, columns, dataVersion, editable, pendingEdits, compact = false, onCellClick, onEdit }: Props) {
  const ref = useRef<DataEditorRef>(null);
  const [widths, setWidths] = useState<Record<string, number>>({});
  const lastRegion = useRef<Rectangle | null>(null);
  const lastY = useRef(0);

  const gridColumns = useMemo<GridColumn[]>(
    () => columns.map((c) => ({ id: c.name, title: c.name, width: widths[c.name] ?? widthFor(c, compact), themeOverride: c.role === "semantic" || c.name.includes(".") ? { bgHeader: "#f8f0e1", textHeader: INK } : undefined })),
    [columns, widths, compact],
  );

  const rows = controller.rowCount();

  const getCellContent = useCallback(
    (cell: Item): GridCell => {
      const [col, row] = cell;
      const c = columns[col];
      if (!c) return { kind: GridCellKind.Loading, allowOverlay: false };
      const { value, truncated, missing } = controller.getCell(row, c.name);
      if (missing) return { kind: GridCellKind.Loading, allowOverlay: false, skeletonWidth: 80, skeletonWidthVariability: 60 };
      const rowId = controller.getRow(row)?._row_id;
      const pendingKey = `${rowId}:${c.name}`;
      const pendingValue = pendingEdits.get(pendingKey);
      const v = pendingEdits.has(pendingKey) ? pendingValue : value;
      const isEditable = editable.has(c.name);
      if (c.name.endsWith(".status") && typeof v === "string") {
        return { kind: GridCellKind.Bubble, data: [v], allowOverlay: false, themeOverride: STATUS_THEME[v] };
      }
      if (v === null || v === undefined) {
        const status = c.name.includes(".") ? (controller.getCell(row, c.name.split(".")[0] + ".status").value as string | undefined) : undefined;
        const label = status === "pending" ? "pending" : status === "uncertain" ? "uncertain" : status === "missing" ? "missing input" : "";
        return { kind: GridCellKind.Text, data: "", displayData: label, allowOverlay: isEditable, readonly: !isEditable, themeOverride: { textDark: TERTIARY }, contentAlign: "left" } as GridCell;
      }
      if (typeof v === "number") {
        const display = Number.isInteger(v) ? v.toLocaleString() : v.toFixed(c.name.endsWith(".score") || c.name.endsWith(".confidence") ? 2 : 3);
        return { kind: GridCellKind.Number, data: v, displayData: display, allowOverlay: false, contentAlign: "right" };
      }
      if (typeof v === "boolean") {
        return { kind: GridCellKind.Text, data: v ? "true" : "false", displayData: v ? "yes" : "no", allowOverlay: isEditable, readonly: !isEditable, themeOverride: v ? { textDark: OK } : { textDark: MUTED }, contentAlign: "left" } as GridCell;
      }
      const s = String(v);
      const display = s.replace(/\s+/g, " ");
      return {
        kind: GridCellKind.Text,
        data: s,
        displayData: truncated ? display + " …" : display,
        allowOverlay: isEditable,
        readonly: !isEditable,
        themeOverride: pendingEdits.has(pendingKey) ? { textDark: LINK, bgCell: "#eeeefb" } : undefined,
      } as GridCell;
    },
    // dataVersion forces Glide to re-read cells after blocks arrive
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [columns, controller, editable, pendingEdits, dataVersion],
  );

  const onVisibleRegionChanged = useCallback(
    (range: Rectangle) => {
      const dir: 1 | -1 = range.y >= lastY.current ? 1 : -1;
      lastY.current = range.y;
      lastRegion.current = range;
      controller.ensureVisible(range.y, range.y + range.height, dir);
    },
    [controller],
  );

  useEffect(() => {
    // After a new view/generation, request the current viewport again.
    const r = lastRegion.current;
    controller.ensureVisible(r ? r.y : 0, r ? r.y + r.height : 60);
  }, [controller, controller.generation, dataVersion]);

  const onCellEdited = useCallback(
    (cell: Item, newValue: EditableGridCell) => {
      const c = columns[cell[0]];
      const row = controller.getRow(cell[1]);
      if (!c || !row || !editable.has(c.name)) return;
      let value: unknown = newValue.kind === GridCellKind.Text ? newValue.data : (newValue as { data?: unknown }).data;
      if (typeof value === "string") {
        const t = value.trim();
        if (t === "") value = null;
        else if (c.type === "boolean" || /^(true|false)$/i.test(t)) value = /^true$/i.test(t);
        else value = t;
      }
      onEdit(row._row_id, c.name, value);
    },
    [columns, controller, editable, onEdit],
  );

  return (
    <DataEditor
      ref={ref}
      columns={gridColumns}
      rows={rows}
      getCellContent={getCellContent}
      onVisibleRegionChanged={onVisibleRegionChanged}
      onCellClicked={(cell) => {
        const c = columns[cell[0]];
        if (c) onCellClick(cell[1], c);
      }}
      onCellEdited={onCellEdited}
      onColumnResize={(col, w) => setWidths((s) => ({ ...s, [col.id ?? col.title]: w }))}
      rowMarkers={compact ? "none" : "number"}
      rowHeight={compact ? 40 : 32}
      headerHeight={compact ? 38 : 34}
      freezeColumns={compact ? 0 : 1}
      smoothScrollX
      smoothScrollY
      getCellsForSelection={(sel) => {
        // Bounded clipboard: at most 10,000 cells; larger transfers go through export.
        const maxCells = 10_000;
        const out: GridCell[][] = [];
        const h = Math.min(sel.height, Math.floor(maxCells / Math.max(1, sel.width)));
        for (let r = 0; r < h; r++) {
          const line: GridCell[] = [];
          for (let c = 0; c < sel.width; c++) line.push(getCellContent([sel.x + c, sel.y + r]));
          out.push(line);
        }
        return out;
      }}
      keybindings={{ search: true }}
      width="100%"
      height="100%"
      theme={{
        accentColor: "#2b80ff",
        accentLight: "#eaf2ff",
        baseFontStyle: "13px",
        headerFontStyle: "500 11px",
        fontFamily: '"Geist Variable", ui-sans-serif, system-ui, sans-serif',
        bgHeader: "#f3f3f5",
        bgHeaderHasFocus: "#ececee",
        bgHeaderHovered: "#ececee",
        textHeader: MUTED,
        textDark: INK,
        textMedium: MUTED,
        textLight: TERTIARY,
        bgCell: "#ffffff",
        bgCellMedium: "#fafafa",
        borderColor: "#ececee",
        horizontalBorderColor: "#f1f1f2",
        cellHorizontalPadding: 10,
        linkColor: LINK,
      }}
    />
  );
}
