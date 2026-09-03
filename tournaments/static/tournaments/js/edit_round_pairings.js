// Round-pairings editor: an editable table of schedule "blocks" (pairing,
// rounds, range, pair-from) that generates the per-round schedule shown,
// read-only, in the table below. Blocks are the source of truth; the server
// expands them (the live preview + the saved round_pairings).

import { TABLE_DEFAULTS, postJson } from "/static/editgrid/js/table_helpers.js";
import { getEditVersion, setEditVersion } from "/static/editgrid/js/edit_version.js";

const GRID_ID = "pairing-blocks-table";
const cfg = (window.editgrids || {})[GRID_ID] || {};
if (getEditVersion(GRID_ID) === undefined) setEditVersion(GRID_ID, cfg.version);

const ROUND_ROBIN = ["RoundRobin", "DoubleRoundRobin", "Charlottesville"];
const isRoundRobin = p => ROUND_ROBIN.includes(p);

// strategyTypes is [{value, label}]: the value is the identifier stored in the
// schedule and sent to the engine, the label is what a director reads. Cells
// hold the value, so both Pairing columns format through this to show the name.
const STRATEGY_LABEL = Object.fromEntries(
    pageData.strategyTypes.map(s => [s.value, s.label]),
);
const labelFor = value => STRATEGY_LABEL[value] ?? value ?? "";
const escapeHtml = text =>
    String(text).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);

// Read-only: the label alone.
const strategyFormatter = cell => escapeHtml(labelFor(cell.getValue()));

// Editable: the label plus a caret. A Tabulator list editor looks exactly like
// text until it is clicked, so without this nobody discovers the strategy is
// a choice -- which is the whole content of the cell.
const strategyChoiceFormatter = cell =>
    `<span class="cell-select">${escapeHtml(labelFor(cell.getValue()))}` +
    `<span class="cell-select-caret" aria-hidden="true">\u25be</span></span>`;

const saveStatus = document.getElementById("rp-save-status");

// --- The generated, read-only per-round table ---------------------------
const previewTable = new Tabulator("#round-pairings-preview-table", {
    ...TABLE_DEFAULTS,
    data: pageData.preview,
    columns: [
        { title: "Round", field: "round", width: 90 },
        { title: "Pairing", field: "pairing", minWidth: 160, formatter: strategyFormatter },
        { title: "Pairs from round", field: "start_round", width: 160 },
    ],
});

// --- The editable blocks table ------------------------------------------
const blocksTable = new Tabulator("#pairing-blocks-table", {
    ...TABLE_DEFAULTS,
    movableRows: true,
    data: pageData.blocks,
    columns: [
        { rowHandle: true, formatter: "handle", headerSort: false, width: 30, frozen: true },
        {
            title: "Pairing", field: "pairing", minWidth: 170,
            formatter: strategyChoiceFormatter,
            editor: "list", editorParams: { values: pageData.strategyTypes, autocomplete: true, listOnEmpty: true },
        },
        {
            title: "Rounds", field: "rounds", width: 100,
            editor: "number", editorParams: { min: 1 },
        },
        // Computed from the cumulative round count; not editable.
        { title: "Range", field: "_range", headerSort: false, width: 110 },
        {
            title: "Pair from", field: "pair_from", width: 130,
            editor: "number", editorParams: { min: 1 },
            editable: cell => !isRoundRobin(cell.getRow().getData().pairing),
            formatter: cell => {
                const d = cell.getRow().getData();
                if (isRoundRobin(d.pairing)) return "—";
                const v = cell.getValue();
                return v != null && v !== "" ? `${v} before` : "";
            },
        },
        {
            title: "", width: 40, headerSort: false, hozAlign: "center",
            formatter: () => "<button type='button' class='row-delete-btn' title='Remove'>×</button>",
            cellClick: (e, cell) => { cell.getRow().delete(); afterChange(); },
        },
    ],
});

function blocks() {
    return blocksTable.getRows().map(r => {
        const d = r.getData();
        return {
            pairing: d.pairing,
            rounds: parseInt(d.rounds) || 0,
            pair_from: parseInt(d.pair_from) || 1,
        };
    });
}

// Recompute each block's range from the cumulative round count (client-side,
// instant). e.g. 3 then 5 rounds -> "1–3", "4–8".
function recomputeRanges() {
    let start = 1;
    blocksTable.getRows().forEach(row => {
        const n = parseInt(row.getData().rounds) || 0;
        let range = "";
        if (n === 1) range = `${start}`;
        else if (n > 1) range = `${start}–${start + n - 1}`;
        row.update({ _range: range });
        start += n;
    });
}

let previewTimer;
function refreshPreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
        postJson({ url: pageData.previewUrl, csrfToken: pageData.csrfToken, payload: { blocks: blocks() } })
            .then(res => { if (res && res.ok && res.body) previewTable.setData(res.body.rows); });
    }, 300);
}

// --- Autosave -----------------------------------------------------------
// Persist the blocks. The save carries an optimistic-concurrency version token
// that the server refreshes on each response, so saves must not overlap: if one
// is requested while another is in flight, queue a single follow-up that runs
// once the fresh token is in hand.
let saving = false;
let saveQueued = false;

function save() {
    if (saving) { saveQueued = true; return; }
    saving = true;
    const payload = { blocks: blocks() };
    const version = getEditVersion(GRID_ID);
    if (version !== undefined) payload._version = version;
    postJson({ url: pageData.saveUrl, csrfToken: pageData.csrfToken, payload, statusEl: saveStatus })
        .then(res => {
            saving = false;
            if (res && res.ok && res.body && typeof res.body.version === "number") {
                setEditVersion(GRID_ID, res.body.version);
            }
            if (saveQueued) { saveQueued = false; save(); }
        });
}

// Debounced so a flurry of edits (and an edit that auto-updates a second cell)
// coalesce into one save.
let saveTimer;
function autoSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 400);
}

function afterChange() {
    recomputeRanges();
    refreshPreview();
    autoSave();
}

blocksTable.on("tableBuilt", recomputeRanges);
blocksTable.on("rowMoved", afterChange);
blocksTable.on("cellEdited", cell => {
    if (cell.getField() === "pairing") {
        const def = pageData.defaultRounds[cell.getValue()];
        if (def != null) cell.getRow().update({ rounds: def });
        cell.getRow().reformat();  // refresh the greyed/active pair-from cell
    }
    afterChange();
});

// --- Showing the editor -------------------------------------------------
// The two tables are the *output* of the method above, so a fresh division does
// not show them: two empty grids next to a "Predefined Pairings" control made
// the method look like a third setting rather than what fills them in.
const scheduleEditor = document.getElementById("schedule-editor");
const methodSelect = document.getElementById("pairing-method");
const roundsControls = document.getElementById("method-rounds-controls");
const generateBtn = document.getElementById("generate-method-btn");
const methodStatus = document.getElementById("method-status");

// Tabulator builds asynchronously, and redrawing before that throws on a null
// element. Both tables are measured while the container has no layout, so the
// redraw is required -- it just has to wait its turn.
const tablesBuilt = Promise.all(
    [blocksTable, previewTable].map(
        table => new Promise(resolve => table.on("tableBuilt", resolve)),
    ),
);

function showEditor() {
    const wasHidden = scheduleEditor.hidden;
    scheduleEditor.hidden = false;
    // Measured at zero width while hidden; without this both tables come back
    // collapsed.
    if (wasHidden) {
        tablesBuilt.then(() => {
            blocksTable.redraw(true);
            previewTable.redraw(true);
        });
    }
}

// A division that already has a schedule shows it straight away -- there is
// nothing to reveal, and hiding saved work behind a button would be worse than
// the problem being fixed.
if ((pageData.blocks || []).length) showEditor();

function syncMethodControls() {
    const custom = methodSelect.value === "custom";
    // Total rounds is an input to generation; Custom generates nothing.
    roundsControls.hidden = custom;
    generateBtn.textContent = custom ? "Define blocks manually" : "Generate Schedule";
    document.querySelectorAll("[data-method]").forEach(el => {
        el.hidden = el.dataset.method !== methodSelect.value;
    });
    methodStatus.textContent = "";
}

methodSelect.addEventListener("change", syncMethodControls);
syncMethodControls();

document.getElementById("add-block-btn").addEventListener("click", () => {
    const pairing = pageData.strategyTypes[0].value;
    blocksTable
        .addRow({ pairing, rounds: pageData.defaultRounds[pairing] || 1, pair_from: 1 })
        .then(afterChange);
});

generateBtn.addEventListener("click", () => {
    const status = methodStatus;

    if (methodSelect.value === "custom") {
        // Reveal only. Existing blocks are deliberately left alone: a division
        // with a saved schedule would otherwise lose it to a button press.
        showEditor();
        status.textContent = (pageData.blocks || []).length
            ? "Editing the existing blocks."
            : "Add a block to start.";
        return;
    }

    const totalRounds = parseInt(document.getElementById("method-total-rounds").value);
    status.textContent = "Generating...";
    postJson({
        url: pageData.methodPreviewUrl,
        csrfToken: pageData.csrfToken,
        payload: {
            method: document.getElementById("pairing-method").value,
            total_rounds: totalRounds,
        },
    }).then(res => {
        if (!res || !res.ok || !res.body) {
            const errors = res && res.body ? res.body.errors || [] : [];
            status.textContent = errors.length ? `Error: ${errors.join("; ")}` : "Error generating schedule.";
            return;
        }
        showEditor();
        blocksTable.setData(res.body.blocks).then(() => {
            previewTable.setData(res.body.rows);
            recomputeRanges();
            autoSave();
            status.textContent = "Generated; review the editable blocks below.";
        });
    });
});

document.getElementById("rp-save-btn").addEventListener("click", save);
