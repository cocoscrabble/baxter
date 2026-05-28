import {
    TABLE_DEFAULTS,
    deleteColumn,
    wireAddRowButton,
    wireSaveButton,
} from "./table_helpers.js";

const table = new Tabulator("#board-table-map-table", {
    ...TABLE_DEFAULTS,
    data: pageData.boardTableMap,
    columns: [
        {
            title: "Board",
            field: "board",
            editor: "number",
            editorParams: { min: 1 },
            width: 120,
            hozAlign: "center",
        },
        {
            title: "Table",
            field: "table",
            editor: "number",
            editorParams: { min: 1 },
            width: 120,
            hozAlign: "center",
        },
        deleteColumn(),
    ],
});

function generateMapping(singleTables, boardCount) {
    const rows = [];
    let board = 1;
    let tableNum = 1;
    // First N boards each get their own table.
    for (let i = 0; i < singleTables && board <= boardCount; i++) {
        rows.push({ board: board, table: tableNum });
        board++;
        tableNum++;
    }
    // Remaining boards: two per table.
    while (board <= boardCount) {
        rows.push({ board: board, table: tableNum });
        board++;
        if (board <= boardCount) {
            rows.push({ board: board, table: tableNum });
            board++;
        }
        tableNum++;
    }
    return rows;
}

document.getElementById("generate-btn").addEventListener("click", function() {
    const singleTables = parseInt(document.getElementById("single-tables").value) || 0;
    const boardCount = parseInt(document.getElementById("board-count").value) || 0;
    if (boardCount < 1) return;
    table.setData(generateMapping(singleTables, boardCount));
});

wireAddRowButton({
    table,
    template: t => {
        const maxBoard = t.getData().reduce((m, r) => Math.max(m, parseInt(r.board) || 0), 0);
        return { board: maxBoard + 1, table: null };
    },
    focusField: "table",
});

wireSaveButton({
    table,
    csrfToken: pageData.csrfToken,
    payloadKey: "rows",
    serializeRow: r => ({
        board: parseInt(r.board) || null,
        table: parseInt(r.table) || null,
    }),
});
