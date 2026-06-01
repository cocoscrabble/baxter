import {
    buildColumns,
    createEditTable,
    serializeRow,
    wireAddRowButton,
    wireSaveButton,
    wireUndoRedo,
} from "/static/editgrid/js/table_helpers.js";

const gridId = "fixed-pairings-table";
const cfg = window.editgrids[gridId];

const table = createEditTable("#fixed-pairings-table", {
    data: cfg.rows,
    columns: buildColumns(gridId),
});

wireAddRowButton({
    table,
    gridId,
    template: { round_number: null, entrant1: null, entrant2: null },
    focusField: "round_number",
});

wireSaveButton({ table, gridId, serializeRow: serializeRow(gridId) });

wireUndoRedo(table, gridId);
