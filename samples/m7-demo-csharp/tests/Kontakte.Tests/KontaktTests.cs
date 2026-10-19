namespace Kontakte.Tests;

public sealed class KontaktTests
{
    [Fact]
    public void Anzeige_mit_Ort() => Assert.Equal("Jürgen Groß (Köln)", new Kontakt("Jürgen Groß", null, "Köln").Anzeige);

    [Fact]
    public void Anzeige_ohne_Ort() => Assert.Equal("Ana", new Kontakt("Ana", "ana@example.org", null).Anzeige);
}
