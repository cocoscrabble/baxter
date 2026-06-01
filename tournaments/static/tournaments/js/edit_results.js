import {
    buildColumns,
    createEditTable,
    serializeRow,
    wireAddRowButton,
    wireSaveButton,
    wireUndoRedo,
} from "/static/editgrid/js/table_helpers.js";

const gridId = "results-table";
const cfg = window.editgrids[gridId];

const table = createEditTable("#results-table", {
    data: cfg.rows,
    columns: buildColumns(gridId),
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

wireSaveButton({ table, gridId, serializeRow: serializeRow(gridId) });

wireUndoRedo(table, gridId);
