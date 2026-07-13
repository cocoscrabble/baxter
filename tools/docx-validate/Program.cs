using DocumentFormat.OpenXml;
using DocumentFormat.OpenXml.Packaging;
using DocumentFormat.OpenXml.Validation;

// docx-validate: validate one or more .docx files against the OOXML schema
// using the Open XML SDK's OpenXmlValidator, plus two semantic checks the schema
// validator does not perform. Word enforces the schema on open (LibreOffice does
// not), so this catches the "Word experienced an error trying to open the file"
// class of bugs that produces a perfectly valid zip.
//
// The semantic checks cover "valid zip, Word still rejects" bugs:
//   - Duplicate drawing-object ids (wp:docPr / …:cNvPr): desktop Word silently
//     renumbers them, but Word for the web rejects the file as corrupt and drops
//     the affected images to "unable to load picture".
//   - graphicData/@uri that disagrees with its child payload's namespace: Word
//     resolves a graphic by matching the uri to the child element's namespace,
//     so a mismatch makes it unable to load the graphic and flag the doc corrupt.
//
// Usage:  docx-validate <file.docx> [more.docx ...]
// Exit code: 0 = all files valid, 1 = validation errors found, 2 = usage/IO error.

const string usage = "Usage: docx-validate <file.docx> [more.docx ...]";
var files = new List<string>();

foreach (var arg in args)
{
    if (arg is "-h" or "--help")
    {
        Console.WriteLine(usage);
        return 0;
    }
    else if (arg.StartsWith('-'))
    {
        Console.Error.WriteLine($"unknown option: {arg}");
        return 2;
    }
    else files.Add(arg);
}

if (files.Count == 0)
{
    Console.Error.WriteLine(usage);
    return 2;
}

// Validate against the newest schema the SDK knows; this is the strictest and
// matches current desktop Word.
var validator = new OpenXmlValidator(DocumentFormat.OpenXml.FileFormatVersions.Microsoft365);
int totalProblems = 0;

foreach (var path in files)
{
    if (!File.Exists(path))
    {
        Console.Error.WriteLine($"{path}: no such file");
        return 2;
    }

    // ValidationErrorInfo.Path is computed lazily from the live element, so it
    // must be read while the package is still open — flatten each problem to
    // plain strings inside the using block before the document is disposed.
    List<string> problems;
    try
    {
        using var doc = WordprocessingDocument.Open(path, false);
        problems = validator.Validate(doc)
            .Select(p => $"  [{p.ErrorType}] {p.Id} at {p.Path?.XPath}\n      {p.Description}")
            .ToList();
        problems.AddRange(DuplicateDrawingIds(doc));
        problems.AddRange(GraphicDataUriMismatches(doc));
    }
    catch (Exception ex)
    {
        // A file so malformed the package won't even open still counts as invalid.
        Console.Error.WriteLine($"{path}: could not open: {ex.Message}");
        totalProblems++;
        continue;
    }

    if (problems.Count == 0)
    {
        Console.WriteLine($"{path}: OK");
        continue;
    }

    Console.WriteLine($"{path}: {problems.Count} problem(s)");
    foreach (var line in problems)
        Console.WriteLine(line);
    totalProblems += problems.Count;
}

return totalProblems == 0 ? 0 : 1;

// Report any drawing-object id (wp:docPr) or DrawingML non-visual id (…:cNvPr)
// that is used more than once. Word requires these to be unique per document;
// the schema does not, so OpenXmlValidator passes a file that duplicates them.
// docPr ids and cNvPr ids are separate id-spaces (Word often reuses the same
// number for a drawing's docPr and its own cNvPr), so each LocalName is checked
// against itself only. All cNvPr flavours (pic:, wps:, a: in group shapes, …)
// share one id-space, so they are grouped by the "cNvPr" LocalName together.
// The document body plus every header and footer root, paired with a label for
// diagnostics. Shared by the semantic checks so each sweeps the same parts.
static IEnumerable<(OpenXmlPartRootElement? Root, string Part)> PartRoots(WordprocessingDocument doc)
{
    var main = doc.MainDocumentPart;
    if (main == null) return Enumerable.Empty<(OpenXmlPartRootElement?, string)>();
    return new (OpenXmlPartRootElement?, string)[] { (main.Document, "document.xml") }
        .Concat(main.HeaderParts.Select(h => ((OpenXmlPartRootElement?)h.Header, "header")))
        .Concat(main.FooterParts.Select(f => ((OpenXmlPartRootElement?)f.Footer, "footer")));
}

static List<string> DuplicateDrawingIds(WordprocessingDocument doc)
{
    var ids =
        from root in PartRoots(doc)
        where root.Root != null
        from el in root.Root!.Descendants<OpenXmlElement>()
        where el.LocalName is "docPr" or "cNvPr"
        let id = el.GetAttributes().FirstOrDefault(a => a.LocalName == "id").Value
        where id != null
        select (el.LocalName, Id: id);

    return ids
        .GroupBy(x => (x.LocalName, x.Id))
        .Where(g => g.Count() > 1)
        .Select(g =>
            $"  [DuplicateId] {g.Key.LocalName} id \"{g.Key.Id}\" used {g.Count()} times\n" +
            "      duplicate drawing-object ids make Word for the web reject the file as corrupt")
        .ToList();
}

// Report any <a:graphicData> whose @uri does not name the namespace of its child
// payload element. The uri tells Word which graphic type follows (a wps shape, a
// picture, a chart, …), and Word resolves the payload by matching it to the
// child's namespace; a mismatch means Word cannot load the graphic and flags the
// file corrupt. The schema only requires @uri to be *some* string, so
// OpenXmlValidator passes it — another "valid zip, Word rejects" bug. An empty
// graphicData (no child) carries no payload to contradict the uri, so it is left
// alone.
static List<string> GraphicDataUriMismatches(WordprocessingDocument doc)
{
    var mismatches =
        from root in PartRoots(doc)
        where root.Root != null
        from gd in root.Root!.Descendants<OpenXmlElement>()
        where gd.LocalName == "graphicData"
        let uri = gd.GetAttributes().FirstOrDefault(a => a.LocalName == "uri").Value
        let child = gd.Elements().FirstOrDefault()
        where child != null && uri != null && child.NamespaceUri != uri
        select (root.Part, Uri: uri, Child: child.LocalName, ChildNs: child.NamespaceUri);

    return mismatches
        .Select(m =>
            $"  [GraphicDataUri] in {m.Part}: graphicData/@uri \"{m.Uri}\"\n" +
            $"      does not match child <{m.Child}> namespace \"{m.ChildNs}\";\n" +
            "      Word cannot resolve the graphic and rejects the file as corrupt")
        .ToList();
}
