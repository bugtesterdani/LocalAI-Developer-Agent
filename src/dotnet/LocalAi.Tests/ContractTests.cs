using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;

namespace LocalAi.Tests;

public sealed class ContractTests
{
    internal static string RepoRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null && !File.Exists(Path.Combine(dir.FullName, "contracts", "testvectors", "canonical.json")))
        {
            dir = dir.Parent;
        }
        return dir?.FullName ?? throw new InvalidOperationException("Repository-Wurzel nicht gefunden");
    }

    private static JsonElement Vectors()
    {
        var text = File.ReadAllText(Path.Combine(RepoRoot(), "contracts", "testvectors", "canonical.json"));
        return JsonDocument.Parse(text).RootElement.Clone();
    }

    [Test("B01")]
    public void Kanonisierung_entspricht_gemeinsamen_Testvektoren()
    {
        foreach (var vector in Vectors().GetProperty("valid").EnumerateArray())
        {
            var name = vector.GetProperty("name").GetString();
            var canonical = CanonicalJson.CanonicalizeText(vector.GetProperty("input").GetString()!);
            Assert.Equal(vector.GetProperty("canonical").GetString(), canonical, $"Vektor '{name}'");
            Assert.Equal(vector.GetProperty("sha256").GetString(), CanonicalJson.Sha256Hex(canonical), $"Hash '{name}'");
        }
    }

    [Test("B01")]
    public void Ungueltige_Eingaben_werden_abgelehnt()
    {
        foreach (var vector in Vectors().GetProperty("invalid").EnumerateArray())
        {
            var input = vector.GetProperty("input").GetString()!;
            Assert.Throws<CanonicalJsonException>(() => CanonicalJson.CanonicalizeText(input), vector.GetProperty("name").GetString());
        }
    }

    [Test("B01")]
    public void Vorschlag_aus_Vektor_ergibt_identischen_Hash_ueber_CSharp_Record()
    {
        var vector = Vectors().GetProperty("valid").EnumerateArray().Single(v => v.GetProperty("name").GetString() == "Vorschlag");
        var proposal = ContractJson.Deserialize<ToolProposal>(vector.GetProperty("input").GetString()!);
        Assert.Equal(vector.GetProperty("sha256").GetString(), ProposalHashing.Sha256(proposal));
    }

    [Test("B01")]
    public void Unbekannte_Felder_werden_abgelehnt()
    {
        Assert.Throws<JsonException>(() => ContractJson.Deserialize<UserDecision>(
            """{"proposalId":"a","proposalSha256":"b","decision":"Approve","extra":1}"""));
        Assert.Throws<JsonException>(() => ContractJson.Deserialize<UserDecision>(
            """{"proposalId":"a","decision":"Approve"}"""), "Pflichtfeld fehlt");
    }

    [Test]
    public void Pfadregeln_lehnen_Ausbrueche_ab()
    {
        string[] bad =
        [
            "..\\secret.txt", "a/../b", "/etc/passwd", "\\\\server\\share\\x", "\\\\?\\C:\\x", "C:\\Windows",
            "datei.txt:stream", "CON", "src/nul.txt", "a//b", "a/./b", "ordner./x", "x\u0001", "",
        ];
        foreach (var path in bad)
        {
            Assert.Throws<BrokerException>(() => PathRules.NormalizeRelative(path), $"Pfad '{path}'");
        }
        Assert.Equal("src/Csv/Parser.cs", PathRules.NormalizeRelative("src\\Csv\\Parser.cs"));
        Assert.Equal("docs/Übernahme.md", PathRules.NormalizeRelative("docs/Übernahme.md"));
    }
}
