namespace Rechner.Tests;

public sealed class RechnerTests
{
    [Fact]
    public void Addieren() => Assert.Equal(5, Rechner.Addieren(2, 3));

    [Fact]
    public void Subtrahieren() => Assert.Equal(-1, Rechner.Subtrahieren(2, 3));

    [Fact]
    public void Multiplizieren() => Assert.Equal(6, Rechner.Multiplizieren(2, 3));
}
