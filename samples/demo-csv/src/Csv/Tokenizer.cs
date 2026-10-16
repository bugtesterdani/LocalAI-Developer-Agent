namespace Demo.Csv;

internal static class Tokenizer
{
    public static IEnumerable<string> Tokens(string line) => line.Split(';');
}
