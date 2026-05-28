// Shared helpers for Tabulator edit-tables.

export const TABLE_DEFAULTS = {
    layout: "fitDataTable",
    keybindings: true,
    selectableRange: 1,
    editTriggerEvent: "dblclick",
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
