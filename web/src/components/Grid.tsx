import { DataEditor, GridCellKind, type GridCell, type GridColumn, type Item, type Rectangle, type EditableGridCell, type DataEditorRef } from "@glideapps/glide-data-grid";
import { forwardRef, useCallback, useEffect, useMemo, useState } from "react";
import type { BlockCache } from "../data/BlockCache";
import type { ColumnDef } from "../types";

export interface GridProps {
  columns: ColumnDef[];
  cache: BlockCache;
  rows: number;
  onActivate?: (rowOrdinal: number, rowId: number, column: string) => void;
  onEdit?: (rowId: number, column: string, value: unknown) => void;
  editable?: boolean;
}

function widthFor(c: ColumnDef): number {
  if (c.name === "_row_id") return 70;
  if (c.name.endsWith(".raw") || c.name.endsWith(".candidates")) return 160;
  if (c.name.endsWith(".status")) return 96;
  if (c.name.endsWith(".value")) return 150;
  if (c.type === "number" || c.type === "integer") return 96;
  if (c.type === "boolean") return 84;
  if (c.type === "date" || c.type === "timestamp") return 130;
  return 260;
}

const STATUS_THEME: Record<string, string> = { ok: "#dcfce7", uncertain: "#fef3c7", failed: "#fee2e2", pending: "#e5e7eb", missing_input: "#e5e7eb", skipped: "#e5e7eb", match: "#dcfce7", non_match: "#f3f4f6", no_candidates: "#e5e7eb" };

/** Canvas grid over the block cache: no network, parsing or search inside render callbacks. */
export const Grid = forwardRef<DataEditorRef, GridProps>(function Grid({ columns, cache, rows, onActivate, onEdit, editable }, ref) {
  const [, force] = useState(0);
  useEffect(() => cache.subscribe(() => force((n) => n + 1)), [cache]);
  const [widths, setWidths] = useState<Record<string, number>>({});
  const gridColumns = useMemo<GridColumn[]>(() => columns.map((c) => ({ id: c.name, title: c.name, width: widths[c.name] ?? widthFor(c), themeOverride: c.name.includes(".") ? { bgCell: "#fbfdff" } : undefined })), [columns, widths]);
  const colIndex = useMemo(() => columns.findIndex((c) => c.name === "_row_id"), [columns]);

  const getCellContent = useCallback((cell: Item): GridCell => {
    const [col, row] = cell;
    const data = cache.get(row);
    const c = columns[col];
    if (!data || !c) return { kind: GridCellKind.Loading, allowOverlay: false };
    const v = data[col];
    if (c.name.endsWith(".status")) {
      const s = v == null ? "pending" : String(v);
      return { kind: GridCellKind.Bubble, data: [s], allowOverlay: false, themeOverride: { bgBubble: STATUS_THEME[s] ?? "#e5e7eb" } };
    }
    if (v === null || v === undefined) {
      const st = statusOf(data, columns, c.name);
      if (st === "pending") return { kind: GridCellKind.Loading, allowOverlay: false, skeletonWidth: 40 };
      return { kind: GridCellKind.Text, data: "", displayData: st === "uncertain" ? "?" : "", allowOverlay: false, readonly: true, themeOverride: st === "uncertain" ? { textDark: "#b45309" } : undefined };
    }
    if (c.type === "boolean") return { kind: GridCellKind.Boolean, data: Boolean(v), allowOverlay: false, readonly: !editable };
    if (c.type === "number" || c.type === "integer") {
      const n = typeof v === "number" ? v : Number(v);
      return { kind: GridCellKind.Number, data: n, displayData: c.type === "integer" ? String(n) : (Number.isInteger(n) ? String(n) : n.toFixed(3)), allowOverlay: true, readonly: !editable || c.name === "_row_id" };
    }
    if (c.name.endsWith(".value") && c.type === "text") return { kind: GridCellKind.Bubble, data: [String(v)], allowOverlay: true };
    const s = typeof v === "string" ? v : String(v);
    return { kind: GridCellKind.Text, data: s, displayData: s.length > 300 ? s.slice(0, 300) + "…" : s.replace(/\n/g, " "), allowOverlay: true, readonly: !editable || c.name.endsWith(".raw") || c.name.endsWith(".candidates") };
  }, [cache, columns, editable]);

  const onVisibleRegionChanged = useCallback((range: Rectangle) => {
    cache.ensure(range.y, range.y + range.height + 8, 1);
  }, [cache]);

  const onCellActivated = useCallback((cell: Item) => {
    const data = cache.get(cell[1]);
    if (!data || !onActivate) return;
    const rid = colIndex >= 0 ? Number(data[colIndex]) : cell[1];
    onActivate(cell[1], rid, columns[cell[0]]?.name);
  }, [cache, onActivate, colIndex, columns]);

  const onCellEdited = useCallback((cell: Item, newValue: EditableGridCell) => {
    const data = cache.get(cell[1]);
    if (!data || !onEdit) return;
    const rid = colIndex >= 0 ? Number(data[colIndex]) : cell[1];
    const c = columns[cell[0]];
    let value: unknown = (newValue as any).data;
    if (c.type === "boolean") value = Boolean(value);
    onEdit(rid, c.name, value);
  }, [cache, onEdit, colIndex, columns]);

  return (
    <DataEditor
      ref={ref}
      columns={gridColumns}
      rows={rows}
      getCellContent={getCellContent}
      onVisibleRegionChanged={onVisibleRegionChanged}
      onCellActivated={onCellActivated}
      onCellEdited={editable ? onCellEdited : undefined}
      onColumnResize={(c, w) => setWidths((s) => ({ ...s, [c.id as string]: w }))}
      rowMarkers="number"
      rowHeight={32}
      headerHeight={34}
      freezeColumns={1}
      smoothScrollX
      smoothScrollY
      width="100%"
      height="100%"
      getCellsForSelection={true}
      keybindings={{ search: true }}
      theme={{ accentColor: "#2563eb", accentLight: "#e8efff", baseFontStyle: "13px", headerFontStyle: "600 12px", bgHeader: "#f8fafc", borderColor: "#e5e7eb", textDark: "#1b1f24", fontFamily: "Inter, system-ui, sans-serif" }}
    />
  );
});

function statusOf(row: unknown[], columns: ColumnDef[], name: string): string | null {
  const dot = name.indexOf(".");
  if (dot < 0) return null;
  const base = name.slice(0, dot);
  const idx = columns.findIndex((c) => c.name === `${base}.status`);
  if (idx < 0) return null;
  const s = row[idx];
  return s == null ? "pending" : String(s);
}
