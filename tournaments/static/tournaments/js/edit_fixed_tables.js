import {
    buildLookup,
    createEditTable,
    deleteColumn,
    lookupColumn,
    wireAddRowButton,
    wireSaveButton,
    wireUndoRedo,
} from "/static/editgrid/js/table_helpers.js";

const gridId = "fixed-tables-table";
const cfg = window.editgrids[gridId];
const entrantLookup = buildLookup(cfg.lookups.entrantValues);
const roundValues = Object.fromEntries(cfg.lookups.roundValues.map(r => [r.value, r.label]));

const table = createEditTable("#fixed-tables-table", {
    data: cfg.rows,
    columns: [
        {
            title: "Round",
            field: "round_number",
            editor: "list",
            editorParams: { values: roundValues, listOnEmpty: true },
            formatter: function(cell) {
                const v = cell.getValue();
                if (v === -1 || v === "-1") return "All";
                return v != null ? String(v) : "";
            },
            width: 100,
            hozAlign: "center",
        },
        lookupColumn({ title: "Player", field: "entrant", lookup: entrantLookup, autocomplete: true, minWidth: 200 }),
        {
            title: "Table",
            field: "table_number",
            editor: "number",
            editorParams: { min: 1 },
            width: 100,
            hozAlign: "center",
        },
        deleteColumn(),
    ],
});

wireAddRowButton({
    table,
    gridId,
    template: { round_number: -1, entrant: null, table_number: null },
    focusField: "round_number",
});

wireSaveButton({
    table,
    gridId,
    serializeRow: r => ({
        round_number: r.round_number != null ? parseInt(r.round_number) : null,
        entrant: parseInt(r.entrant) || null,
        table_number: parseInt(r.table_number) || null,
    }),
});

wireUndoRedo(table, gridId);
