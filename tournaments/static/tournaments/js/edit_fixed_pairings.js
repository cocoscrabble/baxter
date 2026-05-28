import {
    TABLE_DEFAULTS,
    buildLookup,
    deleteColumn,
    lookupColumn,
    wireAddRowButton,
    wireSaveButton,
} from "./table_helpers.js";

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
    payloadKey: "pairings",
    serializeRow: r => ({
        round_number: parseInt(r.round_number) || null,
        entrant1: parseInt(r.entrant1) || null,
        entrant2: parseInt(r.entrant2) || null,
    }),
});
