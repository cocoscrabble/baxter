import { TABLE_DEFAULTS, deleteColumn, buildLookup, editAndFocus, wireSaveButton } from "./table_helpers.js";

const entrantLookup = buildLookup(pageData.entrantValues);

const table = new Tabulator("#fixed-pairings-table", {
    ...TABLE_DEFAULTS,
    data: pageData.fixedPairings,
    columns: [
        {
            title: "Round",
            field: "round_number",
            editor: "number",
            editorParams: { min: 1 },
            width: 80,
            hozAlign: "center",
        },
        {
            title: "Player 1",
            field: "entrant1",
            editor: "list",
            editorParams: { values: entrantLookup, autocomplete: true, listOnEmpty: true },
            formatter: cell => entrantLookup[cell.getValue()] || "",
        },
        {
            title: "Player 2",
            field: "entrant2",
            editor: "list",
            editorParams: { values: entrantLookup, autocomplete: true, listOnEmpty: true },
            formatter: cell => entrantLookup[cell.getValue()] || "",
        },
        deleteColumn(),
    ],
});

document.getElementById("add-row-btn").addEventListener("click", function() {
    table.addRow({ round_number: null, entrant1: null, entrant2: null })
        .then(row => editAndFocus(row, "round_number"));
});

wireSaveButton({
    table,
    csrfToken: pageData.csrfToken,
    payloadKey: "pairings",
    serializeRow: r => ({
        round_number: parseInt(r.round_number) || null,
        entrant1: parseInt(r.entrant1) || null,
        entrant2: parseInt(r.entrant2) || null,
    }),
});
