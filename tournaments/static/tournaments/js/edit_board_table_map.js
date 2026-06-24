import { initGrid } from "/static/editgrid/js/grid.js";
import { tagRows, markDirty } from "/static/editgrid/js/table_helpers.js";

const gridId = "board-table-map-table";
const table = initGrid(gridId);

// Build board->table rows for three sections: streamed (top boards, one game
// per table), single (next boards, one game per table), and double (the rest,
// two games per table). `table` is the integer order index; `label` is the
// display string. With "separate" numbering, streamed tables are labelled
// S1, S2, … and the ordinary tables restart at 1; with "continuous" numbering
// every table is numbered in one sequence.
function generateMapping(streamedTables, singleTables, boardCount, numbering) {
    const separate = numbering === "separate";
    const rows = [];
    let board = 1;
    let order = 1;          // integer order index, always increasing
    let ordinary = 1;       // label counter for non-streamed tables

    // Streamed section: one board per table.
    for (let i = 0; i < streamedTables && board <= boardCount; i++) {
        const label = separate ? "S" + (i + 1) : String(order);
        rows.push({ board: board, table: order, label: label });
        board++;
        order++;
    }
    // Single section: one board per table.
    for (let i = 0; i < singleTables && board <= boardCount; i++) {
        rows.push({ board: board, table: order, label: String(separate ? ordinary : order) });
        board++;
        order++;
        ordinary++;
    }
    // Double section: two boards per table.
    while (board <= boardCount) {
        const label = String(separate ? ordinary : order);
        rows.push({ board: board, table: order, label: label });
        board++;
        if (board <= boardCount) {
            rows.push({ board: board, table: order, label: label });
            board++;
        }
        order++;
        ordinary++;
    }
    return rows;
}

document.getElementById("generate-btn").addEventListener("click", function() {
    const streamedTables = parseInt(document.getElementById("streamed-tables").value) || 0;
    const singleTables = parseInt(document.getElementById("single-tables").value) || 0;
    const boardCount = parseInt(document.getElementById("board-count").value) || 0;
    const numbering = document.getElementById("streamed-numbering").value;
    if (boardCount < 1) return;
    table.setData(tagRows(generateMapping(streamedTables, singleTables, boardCount, numbering)));
    markDirty(gridId);
});
