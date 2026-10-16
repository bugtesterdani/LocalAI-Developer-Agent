namespace Demo.Csv;

public sealed class Parser
{
    // TODO: Anführungszeichen und Unicode-Grenzfälle behandeln
    public string[] ParseLine(string line) => line.TrimEnd('\r').Split(';');
}
