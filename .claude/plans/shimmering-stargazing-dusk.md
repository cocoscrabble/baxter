# Inline fixed pairings on the pairings tab

## Context

On the pairings tab, a tournament director generating a pairable round currently has
no way to pin a specific matchup. To set a fixed pairing they must leave the tab, use
the standalone Tabulator editor at `/fixed-pairings/`, then come back and regenerate.
We want fixed-pairing management for the *current* round to live right above that
round's generated table, so the loop is: see the round → pin/unpin a matchup → the
round regenerates in place with no page reload.

Most of the backend already exists and is tested but is **not wired into any UI**:

- `add_fixed_pairing(division, round, e1, e2)` in `tournaments/fixed_pairings.py:23`
  validates, locks rounds with results, redrafts published rounds, creates the
  `FixedPairing`, and **regenerates** — all in one call.
- `AddFixedPairingView` / `RemoveFixedPairingsView` (`tournaments/views.py:318`,
  `:330`) and routes `add_fixed_pairing` / `remove_fixed_pairings`
  (`tournaments/urls.py`) exist but only do full-page redirects and surface errors
  via Django `messages` (reload-only).

Decisions taken with the user:
- **Auto-regenerate (live):** every add/delete regenerates immediately and live-swaps
  the round. Reuse the existing helpers unchanged. Revisit only if latency shows.
- **Both editors coexist:** keep the standalone Tabulator grid; add the inline
  per-round section as the quick path.

The page already uses Datastar: tab switches `@get` the `round_pairings_tab` endpoint
and morph `#pairings-area` (`_round_tab_content.html`); the simulate buttons `@post`
with a `payload` read server-side via `read_signals`. We follow those exact patterns.

## Desired UX

For the selected round, when `can_edit` **and** `selected_status == 'pairable'`, render
a "Fixed pairings (round N)" section **above** the generated pairings table:

- One row per existing fixed pairing for that round: `Name vs Name  [✕]`. The `✕`
  `@post`s a single-delete endpoint; the round regenerates and the fragment swaps.
- An add form: two entrant `<select>`s (bound to signals `fixedFirst`/`fixedSecond`)
  + an **Add** button. Add is client-side disabled when either is empty or they are
  equal (`data-attr-disabled`), so the degenerate cases never reach the server.
- An inline error area (`fixed_error`) for server-side rejections that can still
  happen (e.g. "One or both players already have a fixed pairing for this round").

Non-editors and non-pairable statuses see no controls (the section is fully gated).

## Implementation

### Backend

1. **`tournaments/fixed_pairings.py`** — add a singular `remove_fixed_pairing(division, fp_id) -> (ok, error)`
   parallel to `add_fixed_pairing`: look up the `FixedPairing` (404/silent if not in
   division), block if its round has results (reuse `rounds_with_results`), delete,
   `revert_published_to_draft([round])`, `regenerate_pairings(division)`. Keep the
   existing plural `remove_fixed_pairings` (keep-id based) for the standalone editor.

2. **`tournaments/pairings_view.py`** — in `PairingsPresenter`, add a cached
   `fixed_for_selected` (list of `FixedPairing` for `selected_round`,
   `select_related("entrant1__player", "entrant2__player")`) and, in `as_context`,
   set `context["fixed_pairings_for_round"]` when `selected_status == 'pairable'`.

3. **`tournaments/views.py`**
   - Add a shared helper `_editor_pairings_context(division, presenter)` that returns
     `{generate_label?, waiting_message?, entrants}` — extracted from the logic now
     inlined in `DivisionPairingsView.get_context_data` (`views.py:356`) so every
     fragment endpoint produces identical controls context.
   - `AddFixedPairingView.post`: read inputs via `read_signals` when `is_datastar`
     (else `request.POST`, as today). Call `add_fixed_pairing`. When datastar, build
     context (nav `can_edit=True`, `active_tab="pairings"`, `PairingsPresenter(division).select(round).as_context()`,
     `_editor_pairings_context`, and `fixed_error=error` on failure) and return
     `fragment_response("tournaments/_pairings_body.html", context)`. When **not**
     datastar, keep the current `messages.error` + `redirect("division_pairings")`
     (preserves existing 302 tests).
   - New `RemoveFixedPairingView` (singular): same shape, reads `fp_id`, calls
     `remove_fixed_pairing`.
   - `RoundPairingsTabView.get`: return `_pairings_body.html` instead of
     `_round_tab_content.html`, and add `_editor_pairings_context` so the controls
     stay consistent after a swap. (It already adds `entrants`.)

4. **`tournaments/urls.py`** — add `remove-fixed-pairing/` → `RemoveFixedPairingView`
   (name `remove_fixed_pairing`, singular). Leave the existing plural route.

### Templates

The swap unit becomes a single wrapper `#pairings-body` so one morph updates **both**
the top controls (the Publish button must appear once auto-regenerate creates drafts)
and the round table.

5. **New `_pairings_controls.html`** — `<div id="pairings-controls">` holding the
   generate/publish/view-published/`waiting_message` block currently inlined in
   `division_pairings.html:8-29`.

6. **New `_pairings_body.html`** — `<div id="pairings-body">` that includes
   `_pairings_controls.html` then `_round_tab_content.html`. This is what all fragment
   endpoints (`RoundPairingsTabView`, add, remove) return and morph.

7. **`division_pairings.html`** — replace the inlined controls + include (lines 8-31)
   with `{% include "tournaments/_pairings_body.html" %}`.

8. **`_round_tab_content.html`** — inside `#round-tab-content`, above the pairable
   table, add `{% if can_edit and selected_status == 'pairable' %}` block: the
   fixed-pairings list (looping `fixed_pairings_for_round`, each with a `✕`
   `@post` to `remove_fixed_pairing` carrying `payload:{fp_id}`), the add form (two
   `<select>`s over `entrants` bound to `fixedFirst`/`fixedSecond`, Add button
   `@post`ing `remove`/`add_fixed_pairing` with `payload:{round, entrant1, entrant2}`
   and `headers:{'X-CSRFToken': $_csrfToken}`), and a `{% if fixed_error %}` notice.
   Mirror the existing simulate-button `@post` syntax already in this file.

### Tests (`tournaments/tests/test_views.py`)

- Datastar add: `@post` with `Datastar-Request` header returns 200 fragment, the
  `FixedPairing` exists, and the rendered fragment lists it.
- Datastar single-delete via `remove_fixed_pairing` removes it and regenerates.
- Error path: adding a second fixed pairing for an already-fixed player returns a
  fragment containing `fixed_error` (not a redirect).
- Gating: fixed section absent for a non-editor and for non-pairable statuses.
- Confirm the existing non-datastar redirect tests (`views.py` 884/913/928 area)
  still pass unchanged.

## Verification

- `uv run python manage.py test tournaments.tests`
- `uv run python manage.py runserver`, open a division's pairings tab on a pairable
  round: add a fixed pairing → table regenerates in place with that matchup pinned and
  the 🔒 marker; delete it → regenerates without it; try a duplicate → inline error,
  no reload. Switch tabs and confirm controls/table stay correct.
