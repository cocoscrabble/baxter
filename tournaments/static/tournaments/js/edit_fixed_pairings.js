import {
    buildLookup,
    createEditTable,
    deleteColumn,
    lookupColumn,
    wireAddRowButton,
    wireSaveButton,
    wireUndoRedo,
} from "/static/editgrid/js/table_helpers.js";

const entrantLookup = buildLookup(pageData.lookups.entrantValues);

const table = createEditTable("#fixed-pairings-table", {
    data: pageData.rows,
    columns: [
        {
            title: "Round",
            field: "round_number",
            editor: "number",
            editorParams: { min: 1 },
            width: 100,
            hozAlign: "center",
        },
        lookupColumn({ title: "Player 1", field: "entrant1", lookup: entrantLookup, autocomplete: true }),
        lookupColumn({ title: "Player 2", field: "entrant2", lookup: entrantLookup, autocomplete: true }),
        deleteColumn(),
    ],
});

wireAddRowButton({
    table,
    template: { round_number: null, entrant1: null, entrant2: null },
    focusField: "round_number",
});

wireSaveButton({
    table,
    csrfToken: pageData.csrfToken,
    payloadKey: "rows",
    version: pageData.version,
    serializeRow: r => ({
        round_number: parseInt(r.round_number) || null,
        entrant1: parseInt(r.entrant1) || null,
        entrant2: parseInt(r.entrant2) || null,
    }),
});

wireUndoRedo(table);
