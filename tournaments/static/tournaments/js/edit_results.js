import { TABLE_DEFAULTS, deleteColumn, buildLookup, wireSaveButton } from "./table_helpers.js";

const entrantLookup = buildLookup(pageData.entrants);

const table = new Tabulator("#results-table", {
    ...TABLE_DEFAULTS,
    data: pageData.results,
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
            editorParams: { values: entrantLookup },
            formatter: cell => entrantLookup[cell.getValue()] || "",
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
            editorParams: { values: entrantLookup },
            formatter: cell => entrantLookup[cell.getValue()] || "",
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
        deleteColumn(),
    ],
});

document.getElementById("add-row-btn").addEventListener("click", function() {
    const maxRound = table.getData().reduce((m, r) => Math.max(m, r.round || 0), 0);
    table.addRow({
        round: maxRound + 1,
        winner: null,
        winner_score: null,
        loser: null,
        loser_score: null,
        winner_started: true,
    });
});

wireSaveButton({
    table,
    csrfToken: pageData.csrfToken,
    payloadKey: "results",
    serializeRow: r => ({
        round: parseInt(r.round) || null,
        winner: parseInt(r.winner) || null,
        winner_score: parseInt(r.winner_score) || null,
        loser: parseInt(r.loser) || null,
        loser_score: parseInt(r.loser_score) || null,
        winner_started: r.winner_started === true || r.winner_started === "true",
    }),
});
