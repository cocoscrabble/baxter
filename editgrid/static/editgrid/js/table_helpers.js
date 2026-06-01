// Shared helpers for Tabulator edit-tables.

import { getEditVersion, setEditVersion } from "./edit_version.js";

export const TABLE_DEFAULTS = {
    // "fitColumns" divides the container width among columns. We deliberately do
    // NOT use "fitDataTable"/"fitData": those measure every cell's content to size
    // the table, which on Firefox can enter a width/scrollbar feedback loop and
    // hang the page (the script-unresponsive dialog). fitColumns never measures
    // content, so it stays stable across browsers.
    layout: "fitColumns",
    keybindings: true,
    selectableRange: 1,
    editTriggerEvent: "dblclick",
    // Fixed row height (matching the 26px rows in tabulator_overrides.css) lets
    // Tabulator skip measuring each row's height. That measurement is a forced
    // synchronous reflow per row; on Firefox it compounds into a multi-second
    // freeze while the table builds. With a known height there is nothing to
    // measure, so the build stays fast across browsers.
    rowHeight: 26,
    // These grids hold at most a few hundred rows, so skip the virtual renderer:
    // its per-row offset-height measurement is a forced reflow that, like the
    // height measurement above, freezes Firefox for seconds. "basic" renders all
    // rows up front with none of that measurement.
    renderVertical: "basic",
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

// --- Change-preview decorations -------------------------------------------
// Compare each row against the snapshot of the data as it loaded and show what
// Save will do: green for added rows and changed cells, red for rows pending
// deletion, plus an action button that deletes / reverts / restores the row.
//
// These are applied as pure DOM updates (classes + the action cell's markup)
// rather than via row.reformat(): reformatting a row inside a cell edit discards
// Tabulator's just-recorded undo-history entry, breaking undo/redo of edits.

const deleteButton =
    "<button type='button' class='row-delete-btn' aria-label='Delete row' title='Delete row'>×</button>";

const restoreButton = title =>
    `<button type='button' class='row-restore-btn' aria-label='${title}' title='${title}'>&#8634;</button>`;

// True when an existing row's cells differ from the loaded snapshot. Added rows
// (no snapshot) and rows already pending deletion are not "edited".
function rowIsEdited(row, original) {
    if (!original) return false;
    const data = row.getData();
    if (data._deleted) return false;
    const orig = original[data._rid];
    if (!orig) return false;
    return Object.keys(orig).some(f => norm(data[f]) !== norm(orig[f]));
}

// The action-column button for a row: restore a deleted row, revert an edited
// row to its loaded values, or (when unchanged) offer to soft-delete it.
function actionButton(row, original) {
    const data = row.getData();
    if (data._deleted) return restoreButton("Restore row");
    if (rowIsEdited(row, original)) return restoreButton("Revert changes");
    return deleteButton;
}

function decorateRow(row, original) {
    const data = row.getData();
    const orig = original ? original[data._rid] : undefined;
    const deleted = !!data._deleted;
    row.getElement().classList.toggle("row-deleted", deleted);
    row.getElement().classList.toggle("row-added", !orig && !deleted);
    row.getCells().forEach(cell => {
        const el = cell.getElement();
        const field = cell.getField();
        if (!field) {                         // action column
            el.innerHTML = actionButton(row, original);
            return;
        }
        const changed = orig && !deleted && norm(data[field]) !== norm(orig[field]);
        el.classList.toggle("cell-changed", !!changed);
    });
}

// Build a Tabulator edit-grid: shared defaults, undo/redo history, and the
// change/add/delete decorations above. `original` (keyed by `_rid`) is stashed
// on the table as `_editOriginal` for the save and delete helpers.
export function createEditTable(selector, { data, columns, ...options }) {
    const rows = tagRows(data || []);
    const original = {};
    rows.forEach(({ _rid, ...rest }) => { original[_rid] = rest; });
    const decorate = row => decorateRow(row, original);

    const table = new Tabulator(selector, {
        ...TABLE_DEFAULTS,
        history: true,
        data: rows,
        columns,
        rowFormatter: decorate,
        ...options,
    });
    table._editOriginal = original;
    // A cell edit can change the whole row's decoration (cells turn green, the
    // delete button becomes a revert button), so re-decorate the row after each
    // interactive edit — without row.reformat(), which would wipe the undo entry.
    table.on("cellEdited", cell => decorate(cell.getRow()));
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
    table.getRows().forEach(row => decorateRow(row, original));
    if (typeof table.clearHistory === "function") table.clearHistory();
}

export const deleteColumn = () => ({
    title: "",
    width: 50,
    hozAlign: "center",
    headerSort: false,
    // Three states: a deleted row offers to be un-deleted; an edited row offers
    // to revert to its loaded values; an unchanged row offers to be soft-deleted
    // (flagged red rather than removed, so the change is previewed and reversible).
    formatter: cell => actionButton(cell.getRow(), cell.getTable()._editOriginal),
    cellClick: function(e, cell) {
        const row = cell.getRow();
        const original = cell.getTable()._editOriginal;
        const data = row.getData();
        if (data._deleted) {
            row.update({ _deleted: false });
        } else if (rowIsEdited(row, original)) {
            row.update({ ...original[data._rid] });
        } else {
            row.update({ _deleted: true });
        }
        decorateRow(row, original);
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

// The per-grid config the server rendered into the registry, keyed by grid id
// (the table's dom id). Carries rows, lookups, version, csrfToken, saveUrl.
export function gridConfig(gridId) {
    return (window.editgrids || {})[gridId] || {};
}

// A grid's controls are tagged data-eg="<gridId>" so several grids can share a
// page; look them up by gridId + action rather than a global element id.
function control(gridId, action) {
    return document.querySelector(
        `[data-eg="${gridId}"][data-eg-action="${action}"]`
    );
}

// Wire the Save button to serialize rows and POST them. Rows flagged for
// deletion are dropped from the payload. The button tracks dirty state: red
// while there are unsaved changes, grey once a save succeeds (see style.css).
// A successful save also re-baselines the grid so all previews clear.
export function wireSaveButton({ table, gridId, serializeRow, beforeSave }) {
    const cfg = gridConfig(gridId);
    const saveBtn = control(gridId, "save");
    const statusEl = control(gridId, "status");
    // Optimistic-concurrency token: sent with each save, refreshed from the
    // server's response so consecutive saves from the same page don't conflict.
    // Held in the shared (keyed) edit-version store so the presence heartbeat
    // can spot when someone else has saved and warn before this user hits Save.
    setEditVersion(gridId, cfg.version);
    table.on("dataChanged", () => saveBtn.classList.add("is-dirty"));
    saveBtn.addEventListener("click", function() {
        if (beforeSave) beforeSave();
        const rows = table.getData()
            .filter(r => !r._deleted)
            .map(serializeRow);
        const payload = { rows };
        const currentVersion = getEditVersion(gridId);
        if (currentVersion !== undefined) payload._version = currentVersion;
        postJson({
            url: cfg.saveUrl || "",
            csrfToken: cfg.csrfToken,
            payload,
            statusEl,
        }).then(result => {
            if (result && result.ok) {
                if (result.body && typeof result.body.version === "number") {
                    setEditVersion(gridId, result.body.version);
                }
                rebaseline(table);
                saveBtn.classList.remove("is-dirty");
                syncUndoRedo(table, gridId);
            }
            // On a conflict the button stays dirty so unsaved edits aren't lost;
            // postJson already shows the server's "reload" message in save-status.
        });
    });
}

// Wire the Add Row button. `template` is a row dict, or a function (table) => dict
// to compute one from current data. Every new row gets a `_rid` so it previews
// as added. If `focusField` is set, the new row's cell for that field is opened.
export function wireAddRowButton({ table, gridId, template, focusField }) {
    control(gridId, "add").addEventListener("click", function() {
        const base = typeof template === "function" ? template(table) : { ...template };
        const promise = table.addRow({ ...base, _rid: nextRid() });
        if (focusField) {
            promise.then(r => editAndFocus(r, focusField));
        }
    });
}

// Wire optional Undo / Redo buttons (data-eg-action undo/redo) to Tabulator's
// history. Ctrl/Cmd+Z and Ctrl/Cmd+Y work natively once `history: true` is set;
// these buttons just make it discoverable and reflect availability.
export function wireUndoRedo(table, gridId) {
    const undoBtn = control(gridId, "undo");
    const redoBtn = control(gridId, "redo");
    if (!undoBtn || !redoBtn) return;
    undoBtn.disabled = true;
    redoBtn.disabled = true;
    undoBtn.addEventListener("click", () => table.undo());
    redoBtn.addEventListener("click", () => table.redo());
    // An undo/redo changes cell values; refresh every row's decoration to match.
    const redecorateAll = () => table.getRows().forEach(r => decorateRow(r, table._editOriginal));
    // dataChanged fires just before the history entry is recorded, so read the
    // sizes on the next tick to reflect the action that just happened.
    const sync = () => setTimeout(() => syncUndoRedo(table, gridId), 0);
    table.on("dataChanged", sync);
    table.on("historyUndo", () => { redecorateAll(); sync(); });
    table.on("historyRedo", () => { redecorateAll(); sync(); });
}

function syncUndoRedo(table, gridId) {
    const undoBtn = control(gridId, "undo");
    const redoBtn = control(gridId, "redo");
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
