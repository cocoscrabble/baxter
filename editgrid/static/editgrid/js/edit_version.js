// The page's current optimistic-concurrency version for its edit grid.
//
// Shared so the Save button (which sends it and refreshes it after each save)
// and the presence heartbeat (which reports it to detect that someone else has
// saved) agree on which version we're holding. One grid per edit page, so a
// single module-level value is sufficient. Undefined until the grid wires up.

let current;

export function getEditVersion() {
    return current;
}

export function setEditVersion(version) {
    current = version;
}
