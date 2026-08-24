import { initGrid } from "/static/editgrid/js/grid.js";
import { lookupMap, nextRid } from "/static/editgrid/js/table_helpers.js";

const gridId = "entrants-table";
const cfg = window.editgrids[gridId];

// Player id -> the rating a new entrant would pin (CoCo, else WESPA, else 0),
// so the table can be kept sorted client-side and a new row can prefill its
// snapshot. Newly created players (below) extend this map. The server re-derives
// the snapshot in prepare() regardless — this is for display and ordering only.
const playerRating = {};
(cfg.lookups.players || []).forEach(p => {
    playerRating[p.id] = p.effective_rating ?? p.rating ?? 0;
});

// An existing entrant sorts on its own pinned rating, which may have been
// hand-edited away from the player's; a brand-new row has none yet, so it falls
// back to what it is about to pin.
function rowRating(row) {
    return row.rating ?? playerRating[row.player] ?? 0;
}

// Number only the surviving rows, so seeds stay sequential once rows marked for
// deletion are dropped on save.
function renumber() {
    let n = 0;
    table.getRows().forEach(row => {
        if (row.getData()._deleted) return;
        row.update({ number: ++n });
    });
}

const byRatingDesc = (a, b) => rowRating(b) - rowRating(a);

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
        row => !row.getData()._deleted && rowRating(row.getData()) < rating
    );
    // No `rating` on the new row: leaving it unset is what tells the server to
    // snapshot the cascade rather than treat it as a hand-typed override.
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

const duplicatePanel = document.getElementById("duplicate-name-panel");
const duplicatePrompt = document.getElementById("duplicate-name-prompt");
const duplicateList = document.getElementById("duplicate-name-list");

function hideDuplicatePanel() {
    duplicatePanel.classList.add("hidden");
    duplicateList.innerHTML = "";
}

// A name that already exists is far more often a typo than two real people, so
// the server refuses an unconfirmed create and hands back who it found. Show
// them with their player numbers: the number is what tells them apart.
function showDuplicatePanel(body, addExisting, createAnyway) {
    duplicatePrompt.textContent =
        body.candidates.length === 1
            ? `A player named “${body.name}” already exists. Did you mean them?`
            : `${body.candidates.length} players are already named “${body.name}”. Did you mean one of them?`;
    duplicateList.innerHTML = "";
    for (const candidate of body.candidates) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = `Add ${candidate.label} (#${candidate.player_number}, ${candidate.rating})`;
        btn.addEventListener("click", () => addExisting(candidate));
        li.appendChild(btn);
        duplicateList.appendChild(li);
    }
    duplicatePanel.classList.remove("hidden");
    document.getElementById("create-anyway-btn").onclick = createAnyway;
    document.getElementById("duplicate-cancel-btn").onclick = hideDuplicatePanel;
}

function createPlayer(confirm) {
    const nameInput = document.querySelector("[data-bind='newPlayerName']");
    const ratingInput = document.querySelector("[data-bind='newPlayerRating']");
    const statusEl = document.getElementById("new-player-status");
    const name = (nameInput.value || "").trim();

    if (!name) {
        statusEl.textContent = "Name is required.";
        return;
    }

    statusEl.textContent = "Creating...";

    function addToGrid(id, label, rating) {
        lookupMap(gridId, "player")[id] = label;
        playerRating[id] = rating ?? 0;
        refreshEntrantOptions();
        insertByRating(id);
        nameInput.value = "";
        ratingInput.value = "0";
        statusEl.textContent = "";
        hideDuplicatePanel();
        nameInput.dispatchEvent(new Event("input"));
        ratingInput.dispatchEvent(new Event("input"));
        document.querySelector("[data-on\\:click='$showNewPlayer = false']").click();
    }

    fetch(entrantsExtra.createPlayerUrl, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": cfg.csrfToken,
        },
        body: JSON.stringify({
            name: name,
            rating: parseInt(ratingInput.value) || 0,
            confirm: !!confirm,
            tournament_slug: entrantsExtra.tournamentSlug,
            division_slug: entrantsExtra.divisionSlug,
        }),
    })
    .then(resp => resp.json().then(body => ({ ok: resp.ok, body })))
    .then(({ ok, body }) => {
        if (ok && body.ok) {
            addToGrid(body.id, body.label, body.rating);
        } else if (body.duplicate_name) {
            statusEl.textContent = "";
            showDuplicatePanel(
                body,
                candidate => addToGrid(candidate.id, candidate.label, candidate.rating),
                () => createPlayer(true),
            );
        } else {
            statusEl.textContent = body.error || "Error creating player.";
        }
    })
    .catch(() => {
        statusEl.textContent = "Network error.";
    });
}

document.getElementById("create-player-btn").addEventListener("click", () => {
    createPlayer(false);
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
