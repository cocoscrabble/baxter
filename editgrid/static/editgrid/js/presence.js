// Soft editing-presence, one heartbeat per grid on the page. Loaded once (ES
// modules dedupe by URL even if several grids include it); it iterates the grid
// registry and starts a heartbeat for each grid that has a presence URL.
//
// Each heartbeat also reports the version that grid is holding, so the server
// can tell us when someone else saved — we warn before the user hits Save and
// hits the conflict. The optimistic-concurrency check is still what actually
// prevents clobbering; this is just the early warning.

import { getEditVersion, setEditVersion } from "./edit_version.js";

const HEARTBEAT_MS = 15000;

function startPresence(gridId, cfg) {
    const banner = document.querySelector(`[data-eg-presence="${gridId}"]`);
    // Seed this grid's version so the first heartbeat can detect staleness even
    // if the user never edits (and regardless of when its grid module runs).
    if (getEditVersion(gridId) === undefined) setEditVersion(gridId, cfg.version);

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
        const version = getEditVersion(gridId);
        let url = cfg.presenceUrl;
        if (version !== undefined) url += "?known_version=" + version;
        fetch(url, { method: "POST", headers: { "X-CSRFToken": cfg.csrfToken } })
            .then(resp => (resp.ok ? resp.json() : null))
            .then(body => { if (body) render(body); })
            .catch(() => { /* transient network error — try again next beat */ });
    }

    // On the way out, drop our presence so the banner clears for others promptly.
    // sendBeacon can't set headers, so the CSRF token rides in the form body.
    function release() {
        const fd = new FormData();
        fd.append("csrfmiddlewaretoken", cfg.csrfToken);
        fd.append("release", "1");
        navigator.sendBeacon(cfg.presenceUrl, fd);
    }

    heartbeat();
    const timer = setInterval(heartbeat, HEARTBEAT_MS);
    window.addEventListener("pagehide", () => { clearInterval(timer); release(); });
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") release();
        else heartbeat();
    });
}

// Stagger grids' heartbeats so several on one page never fire at the same
// instant (which collides on a write-serialized DB like SQLite). A fixed
// per-grid offset keeps their 15s cycles permanently out of phase.
let gridIndex = 0;
for (const [gridId, cfg] of Object.entries(window.editgrids || {})) {
    if (!cfg.presenceUrl) continue;
    setTimeout(() => startPresence(gridId, cfg), gridIndex * 500);
    gridIndex++;
}
