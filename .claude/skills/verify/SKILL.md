---
name: verify
description: Drive the Baxter Django app end-to-end to observe a change at its real surface (UI, pairing flow), on an isolated DB so the dev database is untouched.
---

# Verifying Baxter changes at runtime

Baxter is a Django app; the surface for most changes is the browser UI. Drive it
on an **isolated SQLite DB** so you never touch the real `db.sqlite3`.

## Isolated server

`baxter/settings.py` reads `DATABASE_URL` (python-decouple), so point it at a temp
DB and run a second server on a spare port:

```bash
export DATABASE_URL="sqlite:///$SCRATCH/verify.db"   # absolute path
uv run python manage.py migrate                       # build the temp DB
# ... bootstrap data (below) ...
uv run python manage.py runserver 127.0.0.1:8001 --noreload   # background it
```

Rust engine changes: run `make rust-engine` first, or the PyO3 extension the
Python side imports is stale (symptom: "unknown pairing strategy").

## Bootstrap data (manage.py shell, same DATABASE_URL)

`create_fake_tournament(user, num_players, num_rounds, name)` builds a fully
simulated tournament but **leaves the last round unpaired** — ideal for driving a
pairing. It samples from existing non-provisional `Player`s, so seed a pool first:

```python
from users.models import User
from tournaments.models import Player
from tournaments.fake_tournament import create_fake_tournament
u = User.objects.create_user(username="verify", password="verifypass123",
                             is_staff=True, is_superuser=True)
Player.objects.bulk_create([Player(name=f"Player {i}", player_number=str(100+i),
    rating=1900-20*i, is_provisional=False) for i in range(12)])
div = create_fake_tournament(u, 8, 6, name="Verify")   # round 6 left unpaired
# tweak div.settings.round_pairings / .cop_config etc. as the change needs
```

## Drive it (chrome-devtools MCP)

URL prefix is `/tournaments/<tournament_slug>/division/<division_slug>/…` (note
**division**, singular). Log in at `/accounts/login/`, then the tab routes:
`settings/`, `round-pairings/`, `pair-rounds/` (lazily pairs the next unpaired
round and shows errors inline), `pairings/`, `standings/`.

## Cleanup

`pkill -f "runserver 127.0.0.1:8001"` and delete the temp DB. The dev
`db.sqlite3` mtime should be unchanged — check it.
