# docx-validate

A small CLI that validates `.docx` files against the OOXML schema using the
Open XML SDK's [`OpenXmlValidator`], **plus** two semantic checks the schema
validator does not perform (duplicate drawing-object ids, and `graphicData`
uris that disagree with their payload). The schema check is the strict validation Microsoft Word
performs when opening a document — LibreOffice does **not** validate, so a file
can open cleanly there yet make Word show *"Word experienced an error trying to
open the file."* A malformed document is still a perfectly valid zip, so a
"does it unzip / does LibreOffice open it" test won't catch these bugs; this
tool does.

We generate scorecards by hand-assembling WordprocessingML
(`tournaments/scorecards.py`), so this guards that output against schema
regressions — most often child elements emitted out of their required sequence
order.

## Duplicate drawing-object ids

Beyond the schema, the tool reports any `wp:docPr/@id` (drawing-object id) or
`…:cNvPr/@id` (DrawingML non-visual id) used more than once in the document.
This is a *semantic* uniqueness constraint the schema validator does not check:
`python-docx`'s `add_picture` emits every `pic:cNvPr` id as `0`, so a document
with more than one image duplicates them. Desktop Word and LibreOffice silently
renumber on open, but **Word for the web rejects the file as corrupt** and drops
the affected images to "unable to load picture" placeholders — a failure the
schema check alone passes clean. `docPr` and `cNvPr` are separate id-spaces
(Word often reuses one number for a drawing's `docPr` and its own `cNvPr`), so
each is only checked against its own kind; duplicates are reported as
`[DuplicateId]` and count as errors (exit `1`).

## graphicData uri / payload mismatch

The tool also reports any `<a:graphicData uri="…">` whose `@uri` does not name
the namespace of its child payload element. The uri tells Word which kind of
graphic follows — a `wps` shape
(`…/2010/wordprocessingShape`), a picture (`…/2006/picture`), a chart, and so
on — and Word loads the payload by matching that uri to the child element's
namespace. Point the uri at the wrong namespace (e.g.
`…/2010/wordprocessingDrawing` for a `wps:wsp` child) and Word **cannot resolve
the graphic and rejects the file as corrupt**, while `OpenXmlValidator` — which
only requires `@uri` to be some string — passes it clean. Mismatches are
reported as `[GraphicDataUri]` and count as errors (exit `1`). An empty
`graphicData` carries no payload to contradict, so it is left alone.

## Requirements

- .NET SDK (`dotnet-sdk`, tested on 10.0). Install on Arch with
  `sudo pacman -S --needed dotnet-sdk`.

## Usage

```bash
# builds on first run, then validates
tools/docx-validate/validate path/to/scorecards.docx
```

Validate a freshly generated division scorecard:

```bash
uv run python manage.py shell -c '
from tournaments.scorecards import ScorecardSpec, make_rounds, render_scorecards
spec = ScorecardSpec(tournament_name="T", tournament_date="D", player_name="Alice",
    rounds=make_rounds(range(1, 7)), starts={1: "1st"}, qr_url="https://x/y")
open("/tmp/sc.docx", "wb").write(render_scorecards([spec]))
'
tools/docx-validate/validate /tmp/sc.docx
```

Output is `OK` per file, or a list of problems (schema errors with the offending
element's XPath and reason, plus any `[DuplicateId]` drawing-id collisions and
`[GraphicDataUri]` payload mismatches). Exit code: `0` all valid, `1` problems
found, `2` usage/IO error.

## Note

The `SchemaOrderTests` in `tournaments/tests/test_scorecards.py` cover the same
class of bug (sequence ordering of `tblPr`/`tcPr`/`anchor` children) with no
external dependency, and run in CI. This tool is the broader, authoritative
check for local use — it validates the *entire* document against the full
schema, not just the elements we build by hand.

[`OpenXmlValidator`]: https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.validation.openxmlvalidator
