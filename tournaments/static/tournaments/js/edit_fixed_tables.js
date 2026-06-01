import {
    buildColumns,
    createEditTable,
    serializeRow,
    wireAddRowButton,
    wireSaveButton,
    wireUndoRedo,
} from "/static/editgrid/js/table_helpers.js";

const gridId = "fixed-tables-table";
const cfg = window.editgrids[gridId];

const table = createEditTable("#fixed-tables-table", {
    data: cfg.rows,
    columns: buildColumns(gridId),
});

wireAddRowButton({
    table,
    gridId,
    template: { round_number: -1, entrant: null, table_number: null },
    focusField: "round_number",
});

wireSaveButton({ table, gridId, serializeRow: serializeRow(gridId) });

wireUndoRedo(table, gridId);
