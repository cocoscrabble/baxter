"use strict";

const table = new Tabulator("#board-table-map-table", {
    data: pageData.boardTableMap,
    layout: "fitDataTable",
    keybindings: true,
    selectableRange: 1,
    editTriggerEvent: "dblclick",
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
        {
            title: "",
            formatter: function() { return "<button type='button' class='row-delete-btn' aria-label='Delete row'>×</button>"; },
            width: 50,
            hozAlign: "center",
            headerSort: false,
            cellClick: function(e, cell) {
                cell.getRow().delete();
            },
        },
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
    const rows = generateMapping(singleTables, boardCount);
    table.setData(rows);
});

document.getElementById("add-row-btn").addEventListener("click", function() {
    const existing = table.getData();
    const maxBoard = existing.reduce((m, r) => Math.max(m, parseInt(r.board) || 0), 0);
    table.addRow({ board: maxBoard + 1, table: null }).then(function(row) {
        const cell = row.getCell("table");
        cell.edit();
        setTimeout(() => {
            const input = cell.getElement().querySelector("input");
            if (input) input.focus();
        }, 0);
    });
});

document.getElementById("save-btn").addEventListener("click", function() {
    const data = table.getData();
    const rows = data.map(r => ({
        board: parseInt(r.board) || null,
        table: parseInt(r.table) || null,
    }));

    const statusEl = document.getElementById("save-status");
    statusEl.textContent = "Saving...";

    fetch("", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": pageData.csrfToken,
        },
        body: JSON.stringify({ rows: rows }),
    })
    .then(resp => resp.json().then(body => ({ ok: resp.ok, body })))
    .then(({ ok, body }) => {
        if (ok && body.ok) {
            statusEl.textContent = "Saved!";
        } else {
            statusEl.textContent = "Error: " + (body.errors || []).join("; ");
        }
    })
    .catch(() => {
        statusEl.textContent = "Network error.";
    });
});
