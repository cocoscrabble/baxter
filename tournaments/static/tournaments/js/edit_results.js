"use strict";

const entrantLookup = {};
pageData.entrants.forEach(e => { entrantLookup[e.id] = e.label; });

const entrantValues = {};
pageData.entrants.forEach(e => { entrantValues[e.id] = e.label; });

const table = new Tabulator("#results-table", {
    data: pageData.results,
    layout: "fitColumns",
    keybindings: true,
    selectableRange: 1,
    editTriggerEvent: "dblclick",
    columns: [
        {
            title: "Round",
            field: "round",
            editor: "number",
            editorParams: { min: 1 },
            width: 80,
        },
        {
            title: "Winner",
            field: "winner",
            editor: "list",
            editorParams: { values: entrantValues },
            formatter: function(cell) {
                return entrantLookup[cell.getValue()] || "";
            },
        },
        {
            title: "W Score",
            field: "winner_score",
            editor: "number",
            editorParams: { min: 0 },
            width: 90,
        },
        {
            title: "Opponent",
            field: "loser",
            editor: "list",
            editorParams: { values: entrantValues },
            formatter: function(cell) {
                return entrantLookup[cell.getValue()] || "";
            },
        },
        {
            title: "Opp Score",
            field: "loser_score",
            editor: "number",
            editorParams: { min: 0 },
            width: 90,
        },
        {
            title: "Started",
            field: "winner_started",
            editor: "list",
            editorParams: { values: { true: "Winner", false: "Opponent" } },
            formatter: function(cell) {
                const v = cell.getValue();
                if (v === true || v === "true") return "Winner";
                if (v === false || v === "false") return "Opponent";
                return "";
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
            },
        },
    ],
});

document.getElementById("add-row-btn").addEventListener("click", function() {
    const data = table.getData();
    let maxRound = 0;
    data.forEach(r => { if (r.round > maxRound) maxRound = r.round; });
    table.addRow({
        round: maxRound + 1,
        winner: null,
        winner_score: null,
        loser: null,
        loser_score: null,
        winner_started: true,
    });
});

document.getElementById("save-btn").addEventListener("click", function() {
    const data = table.getData();
    const rows = data.map(r => ({
        round: parseInt(r.round) || null,
        winner: parseInt(r.winner) || null,
        winner_score: parseInt(r.winner_score) || null,
        loser: parseInt(r.loser) || null,
        loser_score: parseInt(r.loser_score) || null,
        winner_started: r.winner_started === true || r.winner_started === "true",
    }));

    const statusEl = document.getElementById("save-status");
    statusEl.textContent = "Saving...";

    fetch("", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": pageData.csrfToken,
        },
        body: JSON.stringify({ results: rows }),
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
