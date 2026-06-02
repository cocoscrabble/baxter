import { initGrid } from "/static/editgrid/js/grid.js";
import { tagRows } from "/static/editgrid/js/table_helpers.js";

const gridId = "board-table-map-table";
const table = initGrid(gridId);

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
    table.setData(tagRows(generateMapping(singleTables, boardCount)));
});
