import {
    TABLE_DEFAULTS,
    buildLookup,
    deleteColumn,
    lookupColumn,
    wireAddRowButton,
    wireSaveButton,
} from "./table_helpers.js";

const entrantLookup = buildLookup(pageData.entrantValues);
const roundValues = Object.fromEntries(pageData.roundValues.map(r => [r.value, r.label]));

const table = new Tabulator("#fixed-tables-table", {
    ...TABLE_DEFAULTS,
    data: pageData.fixedTables,
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
            width: 80,
            hozAlign: "center",
        },
        lookupColumn({ title: "Player", field: "entrant", lookup: entrantLookup, autocomplete: true }),
        {
            title: "Table",
            field: "table_number",
            editor: "number",
            editorParams: { min: 1 },
            width: 80,
            hozAlign: "center",
        },
        deleteColumn(),
    ],
});

wireAddRowButton({
    table,
    template: { round_number: -1, entrant: null, table_number: null },
    focusField: "round_number",
});

wireSaveButton({
    table,
    csrfToken: pageData.csrfToken,
    payloadKey: "tables",
    serializeRow: r => ({
        round_number: r.round_number != null ? parseInt(r.round_number) : null,
        entrant: parseInt(r.entrant) || null,
        table_number: parseInt(r.table_number) || null,
    }),
});
