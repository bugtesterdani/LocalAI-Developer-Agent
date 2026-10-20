using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace LocalAi.Sandbox;

/// <summary>
/// Offline-Bausteine für den Linux-Gast (M8), befüllt von scripts/sandbox/Get-LocalAiLinuxPrerequisites.ps1:
/// <c>prerequisites.json</c> mit Python für Linux (python-build-standalone, „install_only“), den Wheels des Workers
/// (pydantic, pydantic_core für Linux …) und dem Debian-Paket hyperv-daemons (KVP-Datenaustausch, Copy-VMFile).
/// Jede Datei steht mit SHA-256 im Manifest und wird vor dem Aufbau geprüft.
/// </summary>
public sealed record LinuxPrerequisites(
    string Directory,
    string Python,
    string PythonVersion,
    IReadOnlyList<string> Wheels,
    IReadOnlyList<string> Debs,
    IReadOnlyDictionary<string, string> Sha256)
{
    public const string ManifestFile = "prerequisites.json";

    /// <summary>Pakete, die der Worker im Gast braucht (Wheel-Dateinamen beginnen damit, Groß-/Kleinschreibung egal).</summary>
    public static readonly IReadOnlyList<string> RequiredWheels = ["pydantic", "pydantic_core", "annotated_types", "typing_extensions", "typing_inspection"];

    private static readonly Regex SafeName = new(@"^[A-Za-z0-9][A-Za-z0-9._+~-]{0,200}(/[A-Za-z0-9][A-Za-z0-9._+~-]{0,200})?$");
    private static readonly Regex Hex64 = new("^[0-9a-f]{64}$");

    /// <param name="hypervisor">Unter Hyper-V ist hyperv-daemons Pflicht, unter QEMU (M10) nicht.</param>
    public static LinuxPrerequisites Load(string directory, Hypervisor hypervisor = Hypervisor.HyperV)
    {
        var path = Path.Combine(directory, ManifestFile);
        if (!File.Exists(path))
        {
            throw new InvalidDataException($"{path} fehlt – scripts\\sandbox\\Get-LocalAiLinuxPrerequisites.ps1 ausführen (lädt Python für Linux, Wheels und hyperv-daemons) – auf einem Linux-Host scripts/linux/get-prerequisites.sh");
        }
        using var doc = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
        var root = doc.RootElement;
        string Str(string name) => root.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString()!
            : throw new InvalidDataException($"{ManifestFile}: {name} fehlt");
        List<string> List(string name) => root.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Array
            ? v.EnumerateArray().Select(e => e.GetString() ?? "").ToList()
            : throw new InvalidDataException($"{ManifestFile}: {name} fehlt");
        var hashes = new Dictionary<string, string>(StringComparer.Ordinal);
        if (root.TryGetProperty("sha256", out var sha) && sha.ValueKind == JsonValueKind.Object)
        {
            foreach (var p in sha.EnumerateObject())
            {
                hashes[p.Name] = p.Value.GetString() ?? "";
            }
        }
        var result = new LinuxPrerequisites(Path.GetFullPath(directory), Str("python"), Str("pythonVersion"), List("wheels"), List("debs"), hashes);
        var problems = result.Validate(hypervisor);
        if (problems.Count > 0)
        {
            throw new InvalidDataException($"{path}: {string.Join("; ", problems)}");
        }
        return result;
    }

    public IEnumerable<string> Files => new[] { Python }.Concat(Wheels).Concat(Debs);

    public List<string> Validate(Hypervisor hypervisor = Hypervisor.HyperV)
    {
        var problems = new List<string>();
        if (!Python.EndsWith(".tar.gz", StringComparison.Ordinal) || !Python.Contains("x86_64-unknown-linux-gnu", StringComparison.Ordinal))
        {
            problems.Add("python: Archiv von python-build-standalone für x86_64-unknown-linux-gnu (.tar.gz) erwartet");
        }
        if (!Regex.IsMatch(PythonVersion, @"^3\.(1[2-9]|[2-9][0-9])\.[0-9]+$"))
        {
            problems.Add("pythonVersion: 3.12 oder neuer erwartet");
        }
        foreach (var wheel in RequiredWheels)
        {
            if (!Wheels.Any(w => Path.GetFileName(w).StartsWith(wheel + "-", StringComparison.OrdinalIgnoreCase)))
            {
                problems.Add($"Wheel für {wheel} fehlt");
            }
        }
        var core = Wheels.FirstOrDefault(w => Path.GetFileName(w).StartsWith("pydantic_core-", StringComparison.OrdinalIgnoreCase));
        if (core is not null && !(core.Contains("manylinux", StringComparison.Ordinal) && core.Contains("x86_64", StringComparison.Ordinal)))
        {
            problems.Add("pydantic_core: Wheel für Linux (manylinux, x86_64) erwartet");
        }
        if (hypervisor == Hypervisor.HyperV && !Debs.Any(d => Path.GetFileName(d).StartsWith("hyperv-daemons_", StringComparison.Ordinal)))
        {
            problems.Add("Debian-Paket hyperv-daemons fehlt (KVP-Datenaustausch und Copy-VMFile im Gast)");
        }
        foreach (var file in Files)
        {
            if (!SafeName.IsMatch(file) || file.Contains("..", StringComparison.Ordinal))
            {
                problems.Add($"unzulässiger Dateiname: {file}");
            }
            else if (!Sha256.TryGetValue(file, out var hash) || !Hex64.IsMatch(hash))
            {
                problems.Add($"sha256 fehlt für {file}");
            }
            else if (!File.Exists(Path.Combine(Directory, file)))
            {
                problems.Add($"Datei fehlt: {file}");
            }
        }
        return problems;
    }

    /// <summary>Kopiert alle Dateien nach <paramref name="target"/> und prüft die Kopie gegen die Hashes (wie bei Toolchains).</summary>
    public void CopyVerified(string target)
    {
        foreach (var file in Files)
        {
            var to = Path.Combine(target, file.Replace('/', Path.DirectorySeparatorChar));
            System.IO.Directory.CreateDirectory(Path.GetDirectoryName(to)!);
            File.Copy(Path.Combine(Directory, file.Replace('/', Path.DirectorySeparatorChar)), to, overwrite: true);
            using var stream = File.OpenRead(to);
            if (Convert.ToHexStringLower(SHA256.HashData(stream)) != Sha256[file])
            {
                throw new InvalidDataException($"Linux-Baustein {file}: Hash weicht von {ManifestFile} ab – Aufbau abgebrochen");
            }
        }
        // Für provision.sh (bash, ohne JSON-Parser): eine Zeile je Datei, "<art> <relativer Pfad>".
        var lines = new[] { $"python {Python}" }.Concat(Wheels.Select(w => $"wheel {w}")).Concat(Debs.Select(d => $"deb {d}"));
        File.WriteAllText(Path.Combine(target, "files.txt"), string.Join("\n", lines) + "\n", new UTF8Encoding(false));
    }

    /// <summary>Für den Fingerabdruck der VM: Hash von prerequisites.json (enthält alle Datei-Hashes).</summary>
    public static string ManifestHash(string? directory)
    {
        var path = directory is null ? null : Path.Combine(directory, ManifestFile);
        return path is not null && File.Exists(path) ? Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(path))) : "fehlt";
    }
}
