import { TABLE_DEFAULTS, deleteColumn, buildLookup, editAndFocus, wireSaveButton } from "./table_helpers.js";

const playerLookup = buildLookup(pageData.players);

function renumber() {
    table.getRows().forEach((row, i) => row.update({ number: i + 1 }));
}

const table = new Tabulator("#entrants-table", {
    ...TABLE_DEFAULTS,
    data: pageData.entrants,
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
            editorParams: { values: playerLookup, autocomplete: true, listOnEmpty: true },
            formatter: cell => playerLookup[cell.getValue()] || "",
        },
        deleteColumn(renumber),
    ],
});

document.getElementById("add-row-btn").addEventListener("click", function() {
    const count = table.getDataCount();
    table.addRow({ number: count + 1, player: null })
        .then(row => editAndFocus(row, "player"));
});

wireSaveButton({
    table,
    csrfToken: pageData.csrfToken,
    payloadKey: "entrants",
    beforeSave: renumber,
    serializeRow: r => ({
        number: r.number,
        player: parseInt(r.player) || null,
    }),
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

    fetch(pageData.createPlayerUrl, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": pageData.csrfToken,
        },
        body: JSON.stringify({
            name: name,
            rating: parseInt(ratingInput.value) || 0,
        }),
    })
    .then(resp => resp.json().then(body => ({ ok: resp.ok, body })))
    .then(({ ok, body }) => {
        if (ok && body.ok) {
            playerLookup[body.id] = body.label;
            const count = table.getDataCount();
            table.addRow({ number: count + 1, player: body.id });
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

    fetch(pageData.bulkImportUrl, {
        method: "POST",
        headers: { "X-CSRFToken": pageData.csrfToken },
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
