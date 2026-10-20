using LocalAi.Contracts;

namespace LocalAi.Sandbox;

/// <summary>
/// Welche VM (Gastbetriebssystem) ein Projekt und seine Toolchains verwenden (M8).
/// <list type="bullet">
/// <item>Heimat-Gast: <c>project.guestOs</c>, sonst <c>sandbox.defaultGuestOs</c> aus config/localai.json (Standard Windows).
/// Dort liegen Snapshots; Lesen, Patches, Diff und Übernahme laufen dort.</item>
/// <item>Toolchain: <c>project.toolchainGuests[name]</c> überschreibt den Projekt-Gast, der Projekt-Gast den Standard.
/// Toolchains haben je Betriebssystem eigene Namen (z. B. <c>dotnet</c> und <c>dotnet-linux</c>); ihr Manifest legt mit
/// <c>guest</c> fest, wo sie laufen. Ein ausdrücklicher Eintrag in <c>toolchainGuests</c>, der dem Manifest widerspricht,
/// ist ein Fehler; ein nur geerbter Gast (Projekt/Standard) weicht dem Manifest.</item>
/// </list>
/// </summary>
public sealed record GuestPlan(GuestOs Home, IReadOnlyDictionary<string, GuestOs> Toolchains, IReadOnlyList<string> Notes,
    GuestOs? OsDebug = null)
{
    /// <summary>Alle VMs, die das Projekt braucht: Heimat-Gast zuerst, dann die der Toolchains und von OsDebug (M9).</summary>
    public IReadOnlyList<GuestOs> Required =>
        new[] { Home }.Concat(Toolchains.Values.Order()).Concat(OsDebug is { } d ? [d] : []).Distinct().ToList();

    public GuestOs GuestOf(string toolchain) => Toolchains.TryGetValue(toolchain, out var guest) ? guest : Home;

    /// <summary>Toolchains, die in der VM <paramref name="guest"/> installiert werden (Projektreihenfolge).</summary>
    public IReadOnlyList<string> ToolchainsFor(GuestOs guest, IEnumerable<string> projectToolchains) =>
        projectToolchains.Where(t => GuestOf(t) == guest).ToList();

    public static GuestPlan Single(GuestOs guest = GuestOs.Windows) =>
        new(guest, new Dictionary<string, GuestOs>(StringComparer.Ordinal), []);
}

public static class GuestSelection
{
    public static string Name(GuestOs guest) => guest == GuestOs.Linux ? "Linux" : "Windows";

    /// <param name="manifestGuest">Gast laut Manifest der registrierten Toolchain; null = unbekannt (nicht registriert).</param>
    /// <exception cref="InvalidDataException">Widersprüchliche oder unbekannte Einträge in toolchainGuests.</exception>
    public static GuestPlan Resolve(ProjectConfig project, GuestOs defaultGuest, Func<string, GuestOs?> manifestGuest)
    {
        var home = project.GuestOs ?? defaultGuest;
        var toolchains = project.Toolchains ?? [];
        var explicitGuests = project.ToolchainGuests ?? new Dictionary<string, GuestOs>();
        var problems = new List<string>();
        foreach (var name in explicitGuests.Keys.Where(k => !toolchains.Contains(k)))
        {
            problems.Add($"toolchainGuests: '{name}' ist keine Toolchain des Projekts (toolchains)");
        }
        var result = new Dictionary<string, GuestOs>(StringComparer.Ordinal);
        var notes = new List<string>();
        foreach (var name in toolchains)
        {
            var declared = manifestGuest(name);
            var hasExplicit = explicitGuests.TryGetValue(name, out var chosen);
            var wanted = hasExplicit ? chosen : home;
            if (declared is null || declared == wanted)
            {
                result[name] = wanted;
            }
            else if (hasExplicit)
            {
                problems.Add($"Toolchain '{name}' läuft laut toolchain.json nur unter {Name(declared.Value)}, toolchainGuests verlangt {Name(chosen)} " +
                             "– Eintrag anpassen oder die Toolchain für das andere System wählen (getrennte Namen, z. B. dotnet-linux)");
            }
            else
            {
                result[name] = declared.Value;
                notes.Add($"Toolchain '{name}' läuft in der {Name(declared.Value)}-VM (laut toolchain.json)");
            }
        }
        if (project.OsDebug?.Build is { } build && result.TryGetValue(build.Toolchain, out var buildGuest) && buildGuest != GuestOs.Linux)
        {
            problems.Add($"osDebug.build.toolchain: '{build.Toolchain}' läuft unter {Name(buildGuest)}, QEMU aber in der Linux-VM – eine Linux-Toolchain wählen");
        }
        if (problems.Count > 0)
        {
            throw new InvalidDataException(string.Join("; ", problems));
        }
        // M9: QEMU für das Betriebssystem-Debugging läuft in der Linux-VM.
        return new GuestPlan(home, result, notes, project.OsDebug is null ? null : GuestOs.Linux);
    }
}
