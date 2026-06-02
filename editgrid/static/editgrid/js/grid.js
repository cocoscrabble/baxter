// Generic editable-grid bootstrap. Builds and wires a Tabulator grid entirely
// from its server-rendered config (columns, lookups, add-row hints), so grids
// with no custom controls need no per-grid JS at all. Grids that do have custom
// controls import `initGrid` from here and call it themselves.

import {
    buildColumns,
    createEditTable,
    gridConfig,
    serializeRow,
    wireAddRowButton,
    wireSaveButton,
    wireUndoRedo,
} from "./table_helpers.js";

// Build a new row from the column spec: auto-increment fields get max+1, others
// take their declared `new_row` default (or null).
function newRow(gridId, table) {
    const out = {};
    for (const c of gridConfig(gridId).columns || []) {
        if (c.auto_increment) {
            const max = table.getData().reduce(
                (m, r) => Math.max(m, parseInt(r[c.field]) || 0), 0
            );
            out[c.field] = max + 1;
        } else {
            out[c.field] = c.new_row != null ? c.new_row : null;
        }
    }
    return out;
}

// Create + wire one grid. `opts.beforeSave` runs before each save (e.g. the
// entrants renumber). Returns the Tabulator instance for custom code to use.
export function initGrid(gridId, opts = {}) {
    const cfg = gridConfig(gridId);
    const table = createEditTable("#" + gridId, {
        data: cfg.rows,
        columns: buildColumns(gridId),
    });
    wireAddRowButton({
        table,
        gridId,
        template: t => newRow(gridId, t),
        focusField: cfg.focusField || undefined,
    });
    wireSaveButton({ table, gridId, serializeRow: serializeRow(gridId), beforeSave: opts.beforeSave });
    wireUndoRedo(table, gridId);
    return table;
}

// Auto-init every grid that opted in (no custom module). Grids with custom
// controls have autoInit=false and call initGrid from their own module.
for (const [gridId, cfg] of Object.entries(window.editgrids || {})) {
    if (cfg.autoInit && cfg.columns) initGrid(gridId);
}
