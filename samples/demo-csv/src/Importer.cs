namespace Demo.Csv;

public sealed class Importer
{
    private readonly Parser _parser = new();

    public IReadOnlyList<string[]> Import(string text) =>
        text.Split('\n', StringSplitOptions.RemoveEmptyEntries).Select(_parser.ParseLine).ToList();
}
