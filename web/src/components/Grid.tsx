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
  onCellClick: (displayIndex: number, column: ColumnInfo) => void;
  onEdit: (rowId: number, column: string, value: unknown) => void;
};

const STATUS_THEME: Record<string, Partial<Theme>> = {
  ok: { textDark: "#1f9d55", bgCell: "#f2fbf5" },
  override: { textDark: "#2f6fed", bgCell: "#eef3ff" },
  uncertain: { textDark: "#d98a0b", bgCell: "#fff8ea" },
  missing: { textDark: "#6b7684", bgCell: "#f3f4f6" },
  pending: { textDark: "#9aa3ad", bgCell: "#f6f7f9" },
  failed: { textDark: "#d64545", bgCell: "#fdecec" },
  input_too_long: { textDark: "#d64545", bgCell: "#fdecec" },
  matched: { textDark: "#1f9d55", bgCell: "#f2fbf5" },
  unmatched: { textDark: "#6b7684", bgCell: "#f3f4f6" },
  no_candidates: { textDark: "#6b7684", bgCell: "#f3f4f6" },
  skipped: { textDark: "#9aa3ad", bgCell: "#f6f7f9" },
};

function widthFor(c: ColumnInfo): number {
  const header = Math.min(260, 24 + c.name.length * 7.2); // wide enough to read the header
  if (c.name === "_row_id") return 70;
  if (c.name.endsWith(".status")) return Math.max(96, header);
  if (c.name.endsWith(".score") || c.name.endsWith(".confidence")) return Math.max(92, header);
  if (c.type === "integer" || c.type === "double") return Math.max(110, header);
  if (c.type === "boolean") return Math.max(90, header);
  if (c.type === "date" || c.type === "timestamp") return Math.max(120, header);
  if (c.role === "semantic" || c.name.endsWith(".value")) return Math.max(170, header);
  return 300;
}

export default function Grid({ controller, columns, dataVersion, editable, pendingEdits, onCellClick, onEdit }: Props) {
  const ref = useRef<DataEditorRef>(null);
  const [widths, setWidths] = useState<Record<string, number>>({});
  const lastRegion = useRef<Rectangle | null>(null);
  const lastY = useRef(0);

  const gridColumns = useMemo<GridColumn[]>(
    () => columns.map((c) => ({ id: c.name, title: c.name, width: widths[c.name] ?? widthFor(c), themeOverride: c.role === "semantic" || c.name.includes(".") ? { bgHeader: "#eef3ff" } : undefined })),
    [columns, widths],
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
        return { kind: GridCellKind.Text, data: "", displayData: label, allowOverlay: isEditable, readonly: !isEditable, themeOverride: { textDark: "#9aa3ad" }, contentAlign: "left" } as GridCell;
      }
      if (typeof v === "number") {
        const display = Number.isInteger(v) ? v.toLocaleString() : v.toFixed(c.name.endsWith(".score") || c.name.endsWith(".confidence") ? 2 : 3);
        return { kind: GridCellKind.Number, data: v, displayData: display, allowOverlay: false, contentAlign: "right" };
      }
      if (typeof v === "boolean") {
        return { kind: GridCellKind.Text, data: v ? "true" : "false", displayData: v ? "true" : "false", allowOverlay: isEditable, readonly: !isEditable, themeOverride: v ? { textDark: "#1f9d55" } : { textDark: "#6b7684" }, contentAlign: "left" } as GridCell;
      }
      const s = String(v);
      const display = s.replace(/\s+/g, " ");
      return {
        kind: GridCellKind.Text,
        data: s,
        displayData: truncated ? display + " …" : display,
        allowOverlay: isEditable,
        readonly: !isEditable,
        themeOverride: pendingEdits.has(pendingKey) ? { textDark: "#2f6fed", bgCell: "#eef3ff" } : undefined,
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
      rowMarkers="number"
      rowHeight={32}
      headerHeight={34}
      freezeColumns={1}
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
      theme={{ accentColor: "#2f6fed", accentLight: "#e8efff", baseFontStyle: "13px", headerFontStyle: "600 12.5px", bgHeader: "#f6f7f9", textHeader: "#1c2430", borderColor: "#e9ecef", cellHorizontalPadding: 8 }}
    />
  );
}
