"use strict";

const entrantLookup = {};
pageData.entrantValues.forEach(e => { entrantLookup[e.id] = e.label; });

const entrantValues = {};
pageData.entrantValues.forEach(e => { entrantValues[e.id] = e.label; });

const table = new Tabulator("#fixed-tables-table", {
    data: pageData.fixedTables,
    layout: "fitColumns",
    keybindings: true,
    selectableRange: 1,
    editTriggerEvent: "dblclick",
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
            title: "Player",
            field: "entrant",
            editor: "list",
            editorParams: { values: entrantValues, autocomplete: true, listOnEmpty: true },
            formatter: function(cell) {
                return entrantLookup[cell.getValue()] || "";
            },
        },
        {
            title: "Table",
            field: "table_number",
            editor: "number",
            editorParams: { min: 1 },
            width: 80,
            hozAlign: "center",
        },
        {
            title: "",
            formatter: function() { return "<button type='button'>X</button>"; },
            width: 50,
            hozAlign: "center",
            headerSort: false,
            cellClick: function(e, cell) {
                cell.getRow().delete();
            },
        },
    ],
});

document.getElementById("add-row-btn").addEventListener("click", function() {
    table.addRow({ round_number: null, entrant: null, table_number: null }).then(function(row) {
        const cell = row.getCell("round_number");
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
        round_number: parseInt(r.round_number) || null,
        entrant: parseInt(r.entrant) || null,
        table_number: parseInt(r.table_number) || null,
    }));

    const statusEl = document.getElementById("save-status");
    statusEl.textContent = "Saving...";

    fetch("", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": pageData.csrfToken,
        },
        body: JSON.stringify({ tables: rows }),
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
