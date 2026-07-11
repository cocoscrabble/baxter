using DocumentFormat.OpenXml.Packaging;
using DocumentFormat.OpenXml.Validation;

// docx-validate: validate one or more .docx files against the OOXML schema
// using the Open XML SDK's OpenXmlValidator. Word enforces this schema on open
// (LibreOffice does not), so this catches the "Word experienced an error trying
// to open the file" class of bugs that produces a perfectly valid zip.
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
