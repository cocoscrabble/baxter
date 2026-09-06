import { initGrid } from "/static/editgrid/js/grid.js";

// The entrants grid edits people who are already entered. It does not add them:
// the registration page is the one way in (see division_register.html), so the
// two "add" panels that used to live here — pick a player, or create one — are
// gone along with the client-side seeding they needed.
//
// **The server owns the numbering now.** This file used to sort the rows by
// rating on load and renumber them 1..n before every save, which was right when
// a number was whatever the grid said. It stopped being right when numbers
// became a seeding the server derives (commands.reseed_entrants): the rows
// arrive in seed order already, and a division whose seeding is *frozen*
// — any round out of draft — would have been silently reseeded by rating on the
// next save of any kind.
//
// What is left here is the bulk CSV import, which the registration page does not
// cover: it is for a file of forty entrants, not for one person at a desk.

const gridId = "entrants-table";
const cfg = window.editgrids[gridId];

initGrid(gridId);

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
