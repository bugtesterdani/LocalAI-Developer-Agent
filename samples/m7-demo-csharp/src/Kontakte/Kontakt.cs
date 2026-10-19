namespace Kontakte;

/// <summary>Ein Kontakt; Email und Ort dürfen fehlen (null).</summary>
public sealed record Kontakt(string Name, string? Email, string? Ort)
{
    /// <summary>Anzeigename „Name (Ort)“ bzw. nur „Name“.</summary>
    public string Anzeige => Ort is null ? Name : $"{Name} ({Ort})";
}
