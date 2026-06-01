import {
    buildLookup,
    createEditTable,
    deleteColumn,
    lookupColumn,
    wireAddRowButton,
    wireSaveButton,
    wireUndoRedo,
} from "/static/editgrid/js/table_helpers.js";

const gridId = "results-table";
const cfg = window.editgrids[gridId];
const entrantLookup = buildLookup(cfg.lookups.entrants);

const table = createEditTable("#results-table", {
    data: cfg.rows,
    columns: [
        {
            title: "Round",
            field: "round",
            editor: "number",
            editorParams: { min: 1 },
            width: 100,
        },
        lookupColumn({ title: "Winner", field: "winner", lookup: entrantLookup }),
        {
            title: "W Score",
            field: "winner_score",
            editor: "number",
            editorParams: { min: 0 },
            width: 120,
        },
        lookupColumn({ title: "Opponent", field: "loser", lookup: entrantLookup }),
        {
            title: "Opp Score",
            field: "loser_score",
            editor: "number",
            editorParams: { min: 0 },
            width: 130,
        },
        {
            title: "Started",
            field: "winner_started",
            editor: "list",
            editorParams: { values: { true: "Winner", false: "Opponent" } },
            width: 120,
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

wireAddRowButton({
    table,
    gridId,
    template: t => {
        const maxRound = t.getData().reduce((m, r) => Math.max(m, r.round || 0), 0);
        return {
            round: maxRound + 1,
            winner: null,
            winner_score: null,
            loser: null,
            loser_score: null,
            winner_started: true,
        };
    },
});

wireSaveButton({
    table,
    gridId,
    serializeRow: r => ({
        round: parseInt(r.round) || null,
        winner: parseInt(r.winner) || null,
        winner_score: parseInt(r.winner_score) || null,
        loser: parseInt(r.loser) || null,
        loser_score: parseInt(r.loser_score) || null,
        winner_started: r.winner_started === true || r.winner_started === "true",
    }),
});

wireUndoRedo(table, gridId);
