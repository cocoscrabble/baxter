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

export const deleteColumn = (onAfterDelete) => ({
    title: "",
    formatter: () => "<button type='button' class='row-delete-btn' aria-label='Delete row'>×</button>",
    width: 50,
    hozAlign: "center",
    headerSort: false,
    cellClick: function(e, cell) {
        cell.getRow().delete();
        if (onAfterDelete) onAfterDelete();
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

// Wire the Save button to serialize rows and POST them.
export function wireSaveButton({ table, csrfToken, payloadKey, serializeRow, beforeSave }) {
    document.getElementById("save-btn").addEventListener("click", function() {
        if (beforeSave) beforeSave();
        const rows = table.getData().map(serializeRow);
        postJson({
            csrfToken,
            payload: { [payloadKey]: rows },
            statusEl: document.getElementById("save-status"),
        });
    });
}

// Wire the Add Row button. `template` is a row dict, or a function (table) => dict
// to compute one from current data. If `focusField` is set, the new row's cell
// for that field is opened in edit mode.
export function wireAddRowButton({ table, template, focusField }) {
    document.getElementById("add-row-btn").addEventListener("click", function() {
        const row = typeof template === "function" ? template(table) : { ...template };
        const promise = table.addRow(row);
        if (focusField) {
            promise.then(r => editAndFocus(r, focusField));
        }
    });
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
