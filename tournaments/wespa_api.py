"""Fetching and reading the WESPA rating list."""

import json
import urllib.error
import urllib.request

from django.conf import settings

# Long enough for a slow link, short enough that a wedged endpoint does not hold
# a request thread open indefinitely. The list is ~700 KB.
FETCH_TIMEOUT = 60


class WespaFetchError(Exception):
    """The list could not be retrieved. The message is shown to an admin."""


class WespaParseError(Exception):
    """The document could not be read as a WESPA list."""


def wespa_endpoint_configured() -> bool:
    """Whether a fetch is possible. Unlike the roster, there is no token."""
    return bool(settings.WESPA_API_URL)


def parse_wespa(raw):
    """Rows of ``{wespa_id, name, country, rating}`` from a WESPA document.

    Accepts bytes, str or an already-decoded object, so the fetched bytes and an
    uploaded file are one code path.

    A row that cannot be read is fatal rather than skipped. A list this size is
    all or nothing: quietly dropping the rows we could not understand would mean
    a player's rating silently failing to update, which is precisely the failure
    this whole mechanism exists to make visible.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8-sig")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WespaParseError(f"Not valid JSON: {exc}") from None

    # The endpoint answers with an object wrapping the list; a bare list is
    # accepted too, since that is what an admin exporting one by hand will most
    # naturally produce.
    if isinstance(raw, dict):
        players = raw.get("players")
        if players is None:
            raise WespaParseError("The document has no 'players' list.")
    elif isinstance(raw, list):
        players = raw
    else:
        raise WespaParseError("Expected a WESPA list, not a bare value.")
    if not isinstance(players, list):
        raise WespaParseError("'players' is not a list.")
    if not players:
        raise WespaParseError("The list is empty.")

    rows, seen = [], set()
    for i, entry in enumerate(players, start=1):
        if not isinstance(entry, dict):
            raise WespaParseError(f"Player {i}: expected an object.")
        try:
            wespa_id = int(entry["playerid"])
        except KeyError, TypeError, ValueError:
            raise WespaParseError(
                f"Player {i}: missing or unreadable playerid."
            ) from None
        name = (entry.get("name") or "").strip()
        if not name:
            raise WespaParseError(f"Player {i} (id {wespa_id}): no name.")
        if wespa_id in seen:
            raise WespaParseError(f"Player id {wespa_id} appears twice.")
        seen.add(wespa_id)
        rating = entry.get("cswrating")
        if rating is not None:
            try:
                rating = int(rating)
            except TypeError, ValueError:
                raise WespaParseError(
                    f"{name} (id {wespa_id}): rating {rating!r} is not a number."
                ) from None
        rows.append(
            {
                "wespa_id": wespa_id,
                "name": name,
                "country": (entry.get("country") or "").strip(),
                "rating": rating,
            }
        )
    return rows


def fetch_wespa(url=None) -> bytes:
    """GET the WESPA list. The *normal* path; the file upload is the offline one.

    Errors are translated into something an admin can act on, because the raw
    ones are not: ``URLError`` does not say "the mirror is down, upload a file
    instead".
    """
    url = url or settings.WESPA_API_URL
    if not url:
        raise WespaFetchError(
            "No WESPA endpoint is configured — set WESPA_API_URL, or upload a "
            "file instead."
        )
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise WespaFetchError(
            f"The WESPA list returned {exc.code} {exc.reason} ({url})."
        ) from None
    except urllib.error.URLError as exc:
        raise WespaFetchError(f"Could not reach {url}: {exc.reason}.") from None
    except TimeoutError:
        raise WespaFetchError(
            f"{url} did not respond within {FETCH_TIMEOUT} seconds."
        ) from None
