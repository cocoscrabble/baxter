"use strict";

const playerLookup = {};
pageData.players.forEach(p => { playerLookup[p.id] = p.label; });

const playerValues = {};
pageData.players.forEach(p => { playerValues[p.id] = p.label; });

const table = new Tabulator("#entrants-table", {
    data: pageData.entrants,
    layout: "fitColumns",
    columns: [
        {
            title: "#",
            field: "number",
            width: 60,
            hozAlign: "center",
        },
        {
            title: "Player",
            field: "player",
            editor: "list",
            editorParams: { values: playerValues },
            formatter: function(cell) {
                return playerLookup[cell.getValue()] || "";
            },
        },
        {
            title: "",
            formatter: function() { return "<button type='button'>X</button>"; },
            width: 50,
            hozAlign: "center",
            headerSort: false,
            cellClick: function(e, cell) {
                cell.getRow().delete();
                renumber();
            },
        },
    ],
});

function renumber() {
    const rows = table.getRows();
    rows.forEach((row, i) => {
        row.update({ number: i + 1 });
    });
}

document.getElementById("add-row-btn").addEventListener("click", function() {
    const count = table.getDataCount();
    table.addRow({ number: count + 1, player: null });
});

document.getElementById("save-btn").addEventListener("click", function() {
    renumber();
    const data = table.getData();
    const rows = data.map(r => ({
        number: r.number,
        player: parseInt(r.player) || null,
    }));

    const statusEl = document.getElementById("save-status");
    statusEl.textContent = "Saving...";

    fetch("", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": pageData.csrfToken,
        },
        body: JSON.stringify({ entrants: rows }),
    })
    .then(resp => resp.json().then(body => ({ ok: resp.ok, body })))
    .then(({ ok, body }) => {
        if (ok && body.ok) {
            statusEl.textContent = "Saved!";
        } else {
            statusEl.textContent = "Error: " + (body.errors || []).join("; ");
        }
    })
    .catch(err => {
        statusEl.textContent = "Network error.";
    });
});
