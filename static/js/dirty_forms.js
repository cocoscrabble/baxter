// Track unsaved changes on standard HTML forms that carry a `.btn-save` button,
// and colour that button to match: grey while the form is pristine, red once a
// field differs from the value the server rendered.
//
// "Dirty" is computed against each field's default (the server-rendered value),
// so it works without snapshots and survives datastar fragment morphs — newly
// inserted forms are handled by the same delegated listeners.
//
// Tabulator edit-grids have their own Save buttons outside any <form>; those are
// tracked in table_helpers.js instead, so this module never sees them.

function fieldIsDirty(el) {
    if (!el.name || el.disabled) return false;
    const type = el.type;
    if (type === "hidden" || type === "submit" || type === "button" ||
        type === "reset" || type === "file") {
        return false;
    }
    if (type === "checkbox" || type === "radio") {
        return el.checked !== el.defaultChecked;
    }
    if (el.tagName === "SELECT") {
        for (const opt of el.options) {
            if (opt.selected !== opt.defaultSelected) return true;
        }
        return false;
    }
    return el.value !== el.defaultValue;
}

function formIsDirty(form) {
    for (const el of form.elements) {
        if (fieldIsDirty(el)) return true;
    }
    return false;
}

function refresh(form) {
    const btn = form.querySelector(".btn-save");
    if (!btn) return;
    btn.classList.toggle("is-dirty", formIsDirty(form));
}

function handle(e) {
    const form = e.target.closest && e.target.closest("form");
    if (form) refresh(form);
}

document.addEventListener("input", handle);
document.addEventListener("change", handle);
