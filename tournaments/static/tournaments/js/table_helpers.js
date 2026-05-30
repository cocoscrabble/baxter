// Shared helpers for Tabulator edit-tables.

export const TABLE_DEFAULTS = {
    layout: "fitDataTable",
    keybindings: true,
    selectableRange: 1,
    editTriggerEvent: "dblclick",
    // Bound the grid to the viewport so its body scrolls internally (with a
    // frozen header row) instead of pushing the Add Row / Save toolbar off-screen.
    maxHeight: "calc(100vh - 200px)",
};

// Every row carries a client-side `_rid` so we can match it against the snapshot
// of the data as it was loaded, regardless of position, edits, or sorting.
let ridSeq = 0;
export const nextRid = () => ++ridSeq;

const norm = v => (v === null || v === undefined ? "" : String(v));

// Tag rows with a fresh `_rid`. Use when replacing the whole dataset (e.g. a
// "generate" button calling setData) so the new rows track correctly.
export function tagRows(rows) {
    return rows.map(r => ({ ...r, _rid: nextRid() }));
}

// Decorate a row to preview what Save will do, comparing live data against the
// loaded snapshot: green for added rows and changed cells, red for rows pending
// deletion. Driven from data via rowFormatter so it survives virtual re-renders.
function makeRowFormatter(original) {
    return function(row) {
        const data = row.getData();
        const el = row.getElement();
        const orig = original[data._rid];
        const deleted = !!data._deleted;
        el.classList.toggle("row-deleted", deleted);
        el.classList.toggle("row-added", !orig && !deleted);
        row.getCells().forEach(cell => {
            const field = cell.getField();
            if (!field) return;  // action columns (delete button) have no field
            const changed = orig && !deleted && norm(data[field]) !== norm(orig[field]);
            cell.getElement().classList.toggle("cell-changed", !!changed);
        });
    };
}

// Build a Tabulator edit-grid: shared defaults, undo/redo history, and the
// change/add/delete decorations above. `original` (keyed by `_rid`) is stashed
// on the table as `_editOriginal` for the save helper to re-baseline after save.
export function createEditTable(selector, { data, columns, ...options }) {
    const rows = tagRows(data || []);
    const original = {};
    rows.forEach(({ _rid, ...rest }) => { original[_rid] = rest; });

    const table = new Tabulator(selector, {
        ...TABLE_DEFAULTS,
        history: true,
        data: rows,
        columns,
        rowFormatter: makeRowFormatter(original),
        ...options,
    });
    table._editOriginal = original;
    return table;
}

// Drop rows marked for deletion for real, then treat the surviving rows as the
// new clean baseline so all change decorations clear. Called after a save.
function rebaseline(table) {
    const original = table._editOriginal;
    if (!original) return;
    table.getRows().forEach(row => {
        if (row.getData()._deleted) row.delete();
    });
    Object.keys(original).forEach(k => delete original[k]);
    table.getRows().forEach(row => {
        const { _rid, _deleted, ...rest } = row.getData();
        original[_rid] = rest;
    });
    table.getRows().forEach(row => row.reformat());
    if (typeof table.clearHistory === "function") table.clearHistory();
}

export const deleteColumn = () => ({
    title: "",
    width: 50,
    hozAlign: "center",
    headerSort: false,
    // Soft delete: flag the row (turning it red) instead of removing it, so the
    // change is previewed and reversible. The button toggles between the two.
    formatter: cell => cell.getRow().getData()._deleted
        ? "<button type='button' class='row-restore-btn' aria-label='Restore row' title='Restore row'>&#8634;</button>"
        : "<button type='button' class='row-delete-btn' aria-label='Delete row' title='Delete row'>×</button>",
    cellClick: function(e, cell) {
        const row = cell.getRow();
        row.update({ _deleted: !row.getData()._deleted });
        row.reformat();
    },
});

// Build {id: label} lookup from an array of {id, label}.
export function buildLookup(items) {
    const o = {};
    items.forEach(it => { o[it.id] = it.label; });
    return o;
}

// Focus the input inside a Tabulator cell after entering edit mode.
export function editAndFocus(row, fieldName) {
    const cell = row.getCell(fieldName);
    cell.edit();
    setTimeout(() => {
        const input = cell.getElement().querySelector("input");
        if (input) input.focus();
    }, 0);
}

// POST table data to the current URL. payload is the body object to JSONify.
// statusEl receives "Saving...", "Saved!", or an error message.
export function postJson({ url = "", csrfToken, payload, statusEl }) {
    if (statusEl) statusEl.textContent = "Saving...";
    return fetch(url, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify(payload),
    })
    .then(resp => resp.json().then(body => ({ ok: resp.ok, body })))
    .then(({ ok, body }) => {
        if (statusEl) {
            statusEl.textContent = ok && body.ok
                ? "Saved!"
                : "Error: " + (body.errors || []).join("; ");
        }
        return { ok: ok && body.ok, body };
    })
    .catch(() => {
        if (statusEl) statusEl.textContent = "Network error.";
        return { ok: false, body: null };
    });
}

// Wire the Save button to serialize rows and POST them. Rows flagged for
// deletion are dropped from the payload. The button tracks dirty state: red
// while there are unsaved changes, grey once a save succeeds (see style.css).
// A successful save also re-baselines the grid so all previews clear.
export function wireSaveButton({ table, csrfToken, payloadKey, serializeRow, beforeSave }) {
    const saveBtn = document.getElementById("save-btn");
    table.on("dataChanged", () => saveBtn.classList.add("is-dirty"));
    saveBtn.addEventListener("click", function() {
        if (beforeSave) beforeSave();
        const rows = table.getData()
            .filter(r => !r._deleted)
            .map(serializeRow);
        postJson({
            csrfToken,
            payload: { [payloadKey]: rows },
            statusEl: document.getElementById("save-status"),
        }).then(result => {
            if (result && result.ok) {
                rebaseline(table);
                saveBtn.classList.remove("is-dirty");
                syncUndoRedo(table);
            }
        });
    });
}

// Wire the Add Row button. `template` is a row dict, or a function (table) => dict
// to compute one from current data. Every new row gets a `_rid` so it previews
// as added. If `focusField` is set, the new row's cell for that field is opened.
export function wireAddRowButton({ table, template, focusField }) {
    document.getElementById("add-row-btn").addEventListener("click", function() {
        const base = typeof template === "function" ? template(table) : { ...template };
        const promise = table.addRow({ ...base, _rid: nextRid() });
        if (focusField) {
            promise.then(r => editAndFocus(r, focusField));
        }
    });
}

// Wire optional Undo / Redo buttons (ids `undo-btn` / `redo-btn`) to Tabulator's
// history. Ctrl/Cmd+Z and Ctrl/Cmd+Y work natively once `history: true` is set;
// these buttons just make it discoverable and reflect availability.
export function wireUndoRedo(table) {
    const undoBtn = document.getElementById("undo-btn");
    const redoBtn = document.getElementById("redo-btn");
    if (!undoBtn || !redoBtn) return;
    undoBtn.disabled = true;
    redoBtn.disabled = true;
    undoBtn.addEventListener("click", () => table.undo());
    redoBtn.addEventListener("click", () => table.redo());
    const reformatAll = () => table.getRows().forEach(r => r.reformat());
    // dataChanged fires just before the history entry is recorded, so read the
    // sizes on the next tick to reflect the action that just happened.
    const sync = () => setTimeout(() => syncUndoRedo(table), 0);
    table.on("dataChanged", sync);
    table.on("historyUndo", () => { reformatAll(); sync(); });
    table.on("historyRedo", () => { reformatAll(); sync(); });
}

function syncUndoRedo(table) {
    const undoBtn = document.getElementById("undo-btn");
    const redoBtn = document.getElementById("redo-btn");
    if (!undoBtn || !redoBtn) return;
    try {
        undoBtn.disabled = table.getHistoryUndoSize() === 0;
        redoBtn.disabled = table.getHistoryRedoSize() === 0;
    } catch (e) {
        // History module not ready yet; buttons stay disabled until first edit.
    }
}

// Factory for a Tabulator column backed by an id -> label lookup.
// Renders the label, edits with a `list` editor over the same values.
export function lookupColumn({ title, field, lookup, autocomplete = false, ...extra }) {
    return {
        title,
        field,
        editor: "list",
        editorParams: { values: lookup, autocomplete, listOnEmpty: autocomplete },
        formatter: cell => lookup[cell.getValue()] || "",
        // Names are data-sized; without a floor an empty grid collapses the
        // column below its header. Callers can override via `extra`.
        minWidth: 150,
        ...extra,
    };
}
