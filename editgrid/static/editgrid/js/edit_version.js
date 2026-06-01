// Per-grid optimistic-concurrency versions, keyed by grid id (the table's
// dom id). Shared so each grid's Save button (which sends its version and
// refreshes it after each save) and its presence heartbeat (which reports it
// to detect that someone else saved) agree on the version they're holding.
// Keyed rather than a single value so multiple grids can coexist on one page.

const versions = new Map();

export function getEditVersion(gridId) {
    return versions.get(gridId);
}

export function setEditVersion(gridId, version) {
    versions.set(gridId, version);
}
