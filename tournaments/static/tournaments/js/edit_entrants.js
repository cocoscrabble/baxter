import { initGrid } from "/static/editgrid/js/grid.js";
import { lookupMap, nextRid } from "/static/editgrid/js/table_helpers.js";

const gridId = "entrants-table";
const cfg = window.editgrids[gridId];

// Player id -> rating, so the table can be kept sorted by rating client-side.
// Newly created players (below) extend this map.
const playerRating = {};
(cfg.lookups.players || []).forEach(p => { playerRating[p.id] = p.rating ?? 0; });

// Number only the surviving rows, so seeds stay sequential once rows marked for
// deletion are dropped on save.
function renumber() {
    let n = 0;
    table.getRows().forEach(row => {
        if (row.getData()._deleted) return;
        row.update({ number: ++n });
    });
}

const byRatingDesc = (a, b) => (playerRating[b.player] ?? 0) - (playerRating[a.player] ?? 0);

// Establish the invariant up front: the table opens in rating order (highest
// first) with sequential seeds. Sorting the loaded rows here (before the grid
// is built) makes this the baseline snapshot, so it shows no "changed"
// decorations. Every other creation path — fresh load, or a bulk import that
// reloads the page — passes back through here, so the table is always sorted on
// arrival and stays sorted as rows are inserted in place below.
cfg.rows.sort(byRatingDesc);
cfg.rows.forEach((r, i) => { r.number = i + 1; });

const table = initGrid(gridId, { beforeSave: renumber });

const saveBtn = document.querySelector(`[data-eg="${gridId}"][data-eg-action="save"]`);

// Insert a new entrant into its rating-sorted slot, then autosave. Clicking the
// Save control reuses the grid's save path (renumber via beforeSave, version
// token, re-baseline), so the new entrant is persisted immediately in the right
// seed position.
function insertByRating(playerId) {
    const rating = playerRating[playerId] ?? 0;
    const target = table.getRows().find(
        row => !row.getData()._deleted && (playerRating[row.getData().player] ?? 0) < rating
    );
    const data = { player: playerId, _rid: nextRid() };
    const added = target ? table.addRow(data, true, target) : table.addRow(data, false);
    return added.then(() => saveBtn.click());
}

// -- Add Entrant (form toggle handled by datastar data-show) --

// Populate the player picker from the column's lookup, sorted by name. New
// players created below extend this same lookup map, so they appear here too.
const entrantSelect = document.getElementById("add-entrant-select");

function refreshEntrantOptions() {
    const players = lookupMap(gridId, "player") || {};
    const entries = Object.entries(players)
        .sort((a, b) => a[1].localeCompare(b[1]));
    entrantSelect.innerHTML = "";
    for (const [id, label] of entries) {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = label;
        entrantSelect.appendChild(opt);
    }
}
refreshEntrantOptions();

document.getElementById("add-entrant-btn").addEventListener("click", function() {
    const statusEl = document.getElementById("add-entrant-status");
    const playerId = parseInt(entrantSelect.value);

    if (!playerId) {
        statusEl.textContent = "Select a player.";
        return;
    }

    statusEl.textContent = "";
    insertByRating(playerId);
    document.querySelector("[data-on\\:click='$showNewEntrant = false']").click();
});

// -- Create New Player (form toggle handled by datastar data-show) --

document.getElementById("create-player-btn").addEventListener("click", function() {
    const nameInput = document.querySelector("[data-bind='newPlayerName']");
    const ratingInput = document.querySelector("[data-bind='newPlayerRating']");
    const statusEl = document.getElementById("new-player-status");
    const name = (nameInput.value || "").trim();

    if (!name) {
        statusEl.textContent = "Name is required.";
        return;
    }

    statusEl.textContent = "Creating...";

    fetch(entrantsExtra.createPlayerUrl, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": cfg.csrfToken,
        },
        body: JSON.stringify({
            name: name,
            rating: parseInt(ratingInput.value) || 0,
        }),
    })
    .then(resp => resp.json().then(body => ({ ok: resp.ok, body })))
    .then(({ ok, body }) => {
        if (ok && body.ok) {
            lookupMap(gridId, "player")[body.id] = body.label;
            playerRating[body.id] = body.rating ?? 0;
            refreshEntrantOptions();
            insertByRating(body.id);
            nameInput.value = "";
            ratingInput.value = "0";
            statusEl.textContent = "";
            nameInput.dispatchEvent(new Event("input"));
            ratingInput.dispatchEvent(new Event("input"));
            document.querySelector("[data-on\\:click='$showNewPlayer = false']").click();
        } else {
            statusEl.textContent = body.error || "Error creating player.";
        }
    })
    .catch(() => {
        statusEl.textContent = "Network error.";
    });
});

// -- Bulk Import --

document.getElementById("bulk-import-form").addEventListener("submit", function(e) {
    e.preventDefault();
    const fileInput = document.getElementById("csv-file");
    const statusEl = document.getElementById("import-status");

    if (!fileInput.files.length) {
        statusEl.textContent = "Please select a file.";
        return;
    }

    const formData = new FormData();
    formData.append("csv_file", fileInput.files[0]);

    statusEl.textContent = "Importing...";

    fetch(entrantsExtra.bulkImportUrl, {
        method: "POST",
        headers: { "X-CSRFToken": cfg.csrfToken },
        body: formData,
    })
    .then(resp => resp.json().then(body => ({ ok: resp.ok, body })))
    .then(({ ok, body }) => {
        if (ok && body.ok) {
            const parts = [];
            if (body.added) parts.push(`${body.added} entrant(s) added`);
            if (body.created && body.created.length) {
                parts.push(`${body.created.length} new player(s) created`);
            }
            if (body.skipped && body.skipped.length) {
                parts.push(`${body.skipped.length} already in division`);
            }
            statusEl.textContent = parts.join(", ") + ". Reload the page to see changes.";
        } else {
            statusEl.innerHTML = "<strong>Errors:</strong><br>" +
                (body.errors || []).join("<br>");
        }
    })
    .catch(() => {
        statusEl.textContent = "Network error.";
    });
});
