import { TABLE_DEFAULTS, deleteColumn, buildLookup, editAndFocus, wireSaveButton } from "./table_helpers.js";

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
        {
            title: "Player",
            field: "entrant",
            editor: "list",
            editorParams: { values: entrantLookup, autocomplete: true, listOnEmpty: true },
            formatter: cell => entrantLookup[cell.getValue()] || "",
        },
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

document.getElementById("add-row-btn").addEventListener("click", function() {
    table.addRow({ round_number: -1, entrant: null, table_number: null })
        .then(row => editAndFocus(row, "round_number"));
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
