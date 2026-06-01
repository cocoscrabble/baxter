// Soft editing-presence: heartbeat to the server every few seconds and show a
// banner listing any other editors currently on the same grid. The heartbeat
// also reports the version we're holding, so the server can tell us when
// someone else has saved — we warn before the user hits Save and hits the
// conflict. The optimistic-concurrency check is still what actually prevents
// clobbering; this is just the early warning. Reads a `presenceConfig` global:
//   { url, csrfToken, version }

import { getEditVersion, setEditVersion } from "./edit_version.js";

const HEARTBEAT_MS = 15000;

const banner = document.getElementById("edit-presence");

// Seed the page's edit version from the server-rendered config so the very
// first heartbeat can detect staleness even if the user never edits anything
// (and regardless of whether the grid module has initialised yet). The save
// button keeps it updated from there.
if (getEditVersion() === undefined) setEditVersion(presenceConfig.version);

function render(body) {
    if (!banner) return;
    if (body && body.stale) {
        banner.textContent =
            "⚠️ Someone else has saved changes to this data. Reload before editing — saving now would be rejected.";
        banner.classList.add("is-stale");
        banner.hidden = false;
        return;
    }
    banner.classList.remove("is-stale");
    const editors = (body && body.editors) || [];
    if (editors.length) {
        banner.textContent = "⚠️ Also editing: " + editors.join(", ");
        banner.hidden = false;
    } else {
        banner.textContent = "";
        banner.hidden = true;
    }
}

function heartbeat() {
    const version = getEditVersion();
    let url = presenceConfig.url;
    if (version !== undefined) url += "?known_version=" + version;
    fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": presenceConfig.csrfToken },
    })
        .then(resp => (resp.ok ? resp.json() : null))
        .then(body => { if (body) render(body); })
        .catch(() => { /* transient network error — try again next beat */ });
}

// On the way out, drop our presence so the banner clears for others promptly.
// sendBeacon can't set headers, so the CSRF token rides in the form body where
// Django's standard CSRF check still finds it.
function release() {
    const fd = new FormData();
    fd.append("csrfmiddlewaretoken", presenceConfig.csrfToken);
    fd.append("release", "1");
    navigator.sendBeacon(presenceConfig.url, fd);
}

heartbeat();
const timer = setInterval(heartbeat, HEARTBEAT_MS);

window.addEventListener("pagehide", () => {
    clearInterval(timer);
    release();
});
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") release();
    else heartbeat();
});
