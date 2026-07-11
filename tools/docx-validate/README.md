# docx-validate

A small CLI that validates `.docx` files against the OOXML schema using the
Open XML SDK's [`OpenXmlValidator`]. This is the strict check Microsoft Word
performs when opening a document — LibreOffice does **not** validate, so a file
can open cleanly there yet make Word show *"Word experienced an error trying to
open the file."* A malformed document is still a perfectly valid zip, so a
"does it unzip / does LibreOffice open it" test won't catch these bugs; this
tool does.

We generate scorecards by hand-assembling WordprocessingML
(`tournaments/scorecards.py`), so this guards that output against schema
regressions — most often child elements emitted out of their required sequence
order.

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

Output is `OK` per file, or a list of problems with the offending element's
XPath and the schema reason. Exit code: `0` all valid, `1` schema errors found,
`2` usage/IO error.

## Note

The `SchemaOrderTests` in `tournaments/tests/test_scorecards.py` cover the same
class of bug (sequence ordering of `tblPr`/`tcPr`/`anchor` children) with no
external dependency, and run in CI. This tool is the broader, authoritative
check for local use — it validates the *entire* document against the full
schema, not just the elements we build by hand.

[`OpenXmlValidator`]: https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.validation.openxmlvalidator
