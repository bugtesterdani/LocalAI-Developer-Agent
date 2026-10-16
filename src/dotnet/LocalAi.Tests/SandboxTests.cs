using System.IO.Compression;
using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>Host-Seite der Sandbox: Policy mit Ordnerrechten, Snapshots, Signatur, Ergebnisprüfung.</summary>
public sealed class SandboxTests : IDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "localai-sbx-" + Guid.NewGuid().ToString("N"));
    private readonly ManualClock _clock = new(new DateTimeOffset(2026, 10, 16, 12, 0, 0, TimeSpan.Zero));

    public SandboxTests() => Directory.CreateDirectory(_dir);

    private static JsonElement Vectors(string file) =>
        JsonDocument.Parse(File.ReadAllText(Path.Combine(ContractTests.RepoRoot(), "contracts", "testvectors", file))).RootElement.Clone();

    // ------------------------------------------------------------------ gemeinsame Vektoren

    [Fact, Trait("Pruefung", "SEC-05")]
    public void Pfadregeln_entsprechen_gemeinsamen_Testvektoren()
    {
        var v = Vectors("paths.json");
        foreach (var valid in v.GetProperty("valid").EnumerateArray())
        {
            Assert.Equal(valid.GetProperty("normalized").GetString(), PathRules.NormalizeRelative(valid.GetProperty("input").GetString()));
        }
        foreach (var invalid in v.GetProperty("invalid").EnumerateArray())
        {
            Assert.Throws<BrokerException>(() => PathRules.NormalizeRelative(invalid.GetString()));
        }
        Assert.Equal(".", PathRules.NormalizeRelative(".", allowRoot: true));
    }

    [Fact, Trait("Pruefung", "B01")]
    public void SnapshotId_und_JobSignatur_entsprechen_Python()
    {
        var v = Vectors("sandbox.json");
        var snap = v.GetProperty("snapshot");
        var files = ContractJson.Deserialize<List<SnapshotFile>>(snap.GetProperty("files"));
        Assert.Equal(snap.GetProperty("snapshotId").GetString(), SnapshotBuilder.ComputeSnapshotId(snap.GetProperty("grantId").GetString()!, files));

        var job = v.GetProperty("job");
        var sandboxJob = ContractJson.Deserialize<SandboxJob>(job.GetProperty("job"));
        var key = Convert.FromHexString(job.GetProperty("keyHex").GetString()!);
        Assert.Equal(job.GetProperty("mac").GetString(), JobSigner.ComputeMac(sandboxJob, key));
    }

    // ------------------------------------------------------------------ Policy mit Ordnerrechten

    private static ProposalRequest Request(string tool, string grantId, string snapshot = "snap-1", string root = "/x",
        params string[] paths) => new(
        SchemaVersion: 1, RunId: "r", TaskId: "t", Agent: AgentRole.Coder, Tool: tool, ToolVersion: "1", Purpose: "Test",
        Arguments: JsonSerializer.SerializeToElement(tool == "Workspace.Snapshot"
            ? new Dictionary<string, string> { ["grantId"] = grantId, ["root"] = root }
            : new Dictionary<string, string> { ["grantId"] = grantId }),
        Targets: paths.Select((p, i) => new ToolTarget($"t{i + 1}", p)).ToList(),
        SnapshotId: snapshot, IdempotencyKey: Guid.NewGuid().ToString("N"));

    private static (ProposalPolicy Policy, SnapshotRegistry Registry) Policy(params FolderGrant[] grants)
    {
        var registry = new SnapshotRegistry();
        var project = new ProjectConfig(1, "p", "P", grants);
        return (new ProposalPolicy(new BrokerOptions(), new ProjectPolicyContext(project, registry)), registry);
    }

    [Fact, Trait("Pruefung", "R09")]
    public void Ohne_Grant_oder_Recht_kein_Zugriff()
    {
        var (policy, registry) = Policy(
            new FolderGrant("nurliste", "/a", [FolderRight.List], true),
            new FolderGrant("lesen", "/b", [FolderRight.List, FolderRight.Read], true));
        registry.Add("snap-1", "nurliste");
        registry.Add("snap-2", "lesen");

        Assert.Equal(BrokerErrorCodes.Forbidden, Assert.Throws<BrokerException>(() => policy.Validate(Request("File.Read", "gibtsnicht", "snap-1", paths: "a.txt"))).Code);
        Assert.Equal(BrokerErrorCodes.Forbidden, Assert.Throws<BrokerException>(() => policy.Validate(Request("File.Read", "nurliste", "snap-1", paths: "a.txt"))).Code);
        policy.Validate(Request("File.List", "nurliste", "snap-1", paths: "."));
        policy.Validate(Request("File.Read", "lesen", "snap-2", paths: "a.txt"));
    }

    [Fact, Trait("Pruefung", "R09")]
    public void Snapshot_muss_zum_Grant_gehoeren_und_root_exakt_passen()
    {
        var (policy, registry) = Policy(
            new FolderGrant("a", "/a", [FolderRight.List, FolderRight.Read], true),
            new FolderGrant("b", "/b", [FolderRight.List, FolderRight.Read], true));
        registry.Add("snap-a", "a");
        Assert.Equal(BrokerErrorCodes.Conflict, Assert.Throws<BrokerException>(() => policy.Validate(Request("File.Read", "b", "snap-a", paths: "x"))).Code);
        Assert.Equal(BrokerErrorCodes.Conflict, Assert.Throws<BrokerException>(() => policy.Validate(Request("File.Read", "a", "unbekannt", paths: "x"))).Code);
        Assert.Equal(BrokerErrorCodes.Forbidden, Assert.Throws<BrokerException>(() => policy.Validate(Request("Workspace.Snapshot", "a", root: "/a/../etc"))).Code);
        policy.Validate(Request("Workspace.Snapshot", "a", root: "/a"));
    }

    [Fact, Trait("Pruefung", "R09")]
    public void Nicht_rekursiver_Grant_erlaubt_keine_Unterordner()
    {
        var (policy, registry) = Policy(new FolderGrant("flach", "/f", [FolderRight.List, FolderRight.Read], false));
        registry.Add("s", "flach");
        policy.Validate(Request("File.Read", "flach", "s", paths: "datei.txt"));
        Assert.Equal(BrokerErrorCodes.Forbidden, Assert.Throws<BrokerException>(() => policy.Validate(Request("File.Read", "flach", "s", paths: "unter/datei.txt"))).Code);
    }

    [Fact, Trait("Pruefung", "R09")]
    public void Projektdatei_lehnt_Delete_und_Execute_ab()
    {
        var path = Path.Combine(_dir, "p.json");
        File.WriteAllText(path, """{"schemaVersion":1,"projectId":"p","name":"P","grants":[{"grantId":"g","root":".","rights":["Read","Execute"],"recursive":true}]}""");
        Assert.Throws<InvalidDataException>(() => ProjectLoader.Load(path, _dir));
    }

    // ------------------------------------------------------------------ Snapshot-Builder

    private FolderGrant CreateFolder(Action<string> fill, bool recursive = true, IReadOnlyList<string>? exclude = null)
    {
        var root = Path.Combine(_dir, "projekt");
        Directory.CreateDirectory(root);
        fill(root);
        return new FolderGrant("g", root, [FolderRight.List, FolderRight.Read], recursive, exclude);
    }

    [Fact, Trait("Pruefung", "SEC-05")]
    public void Snapshot_enthaelt_Dateien_und_ueberspringt_Ausschluesse()
    {
        var grant = CreateFolder(root =>
        {
            Directory.CreateDirectory(Path.Combine(root, "src", "Csv"));
            Directory.CreateDirectory(Path.Combine(root, "bin"));
            Directory.CreateDirectory(Path.Combine(root, "geheim"));
            File.WriteAllText(Path.Combine(root, "README.md"), "# Hallo");
            File.WriteAllText(Path.Combine(root, "src", "Csv", "Größe.cs"), "class G {}");
            File.WriteAllText(Path.Combine(root, "bin", "app.dll"), "binär");
            File.WriteAllText(Path.Combine(root, "geheim", "key.txt"), "x");
        }, exclude: ["geheim"]);
        var archive = Path.Combine(_dir, "a.zip");
        var manifest = new SnapshotBuilder().Build(grant, archive, _clock, TestContext.Current.CancellationToken);

        Assert.Equal(["README.md", "src/Csv/Größe.cs"], manifest.Files.Select(f => f.Path).ToArray());
        Assert.Equal(SnapshotBuilder.ComputeSnapshotId("g", manifest.Files), manifest.SnapshotId);
        using var zip = ZipFile.OpenRead(archive);
        Assert.Equal(["README.md", "src/Csv/Größe.cs"], zip.Entries.Select(e => e.FullName).ToArray());
    }

    [Fact, Trait("Pruefung", "SEC-05")]
    public void Verknuepfungen_werden_nicht_verfolgt()
    {
        var outside = Path.Combine(_dir, "ausserhalb");
        Directory.CreateDirectory(outside);
        File.WriteAllText(Path.Combine(outside, "geheim.txt"), "GEHEIM");
        FolderGrant grant;
        try
        {
            grant = CreateFolder(root =>
            {
                File.WriteAllText(Path.Combine(root, "ok.txt"), "ok");
                File.CreateSymbolicLink(Path.Combine(root, "link.txt"), Path.Combine(outside, "geheim.txt"));
                Directory.CreateSymbolicLink(Path.Combine(root, "linkdir"), outside);
            });
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
        {
            Assert.Skip($"Symbolische Links nicht erlaubt (unter Windows Entwicklermodus nötig): {ex.Message}");
            return;
        }
        var manifest = new SnapshotBuilder().Build(grant, Path.Combine(_dir, "a.zip"), _clock, TestContext.Current.CancellationToken);
        Assert.Equal(["ok.txt"], manifest.Files.Select(f => f.Path).ToArray());
        Assert.Equal(2, manifest.Skipped!.Count(s => s.Contains("Verknüpfung")));
    }

    [Fact]
    public void Snapshot_Limits_greifen()
    {
        var grant = CreateFolder(root =>
        {
            for (var i = 0; i < 5; i++)
            {
                File.WriteAllText(Path.Combine(root, $"f{i}.txt"), new string('x', 100));
            }
        });
        Assert.Throws<SnapshotException>(() => new SnapshotBuilder(new SnapshotLimits { MaxFiles = 3 }).Build(grant, Path.Combine(_dir, "a.zip"), _clock, TestContext.Current.CancellationToken));
        var manifest = new SnapshotBuilder(new SnapshotLimits { MaxFileBytes = 50 }).Build(grant, Path.Combine(_dir, "b.zip"), _clock, TestContext.Current.CancellationToken);
        Assert.Empty(manifest.Files);
        Assert.Equal(5, manifest.Skipped!.Count);
    }

    // ------------------------------------------------------------------ Ergebnisprüfung

    private static SandboxJob Job(params string[] targets)
    {
        var now = DateTimeOffset.UtcNow;
        var proposal = new ToolProposal(1, "p1", "r", "t", AgentRole.Coder, "File.ReadMany", "1", "x",
            JsonDocument.Parse("""{"grantId":"g"}""").RootElement.Clone(), targets.Select(t => new ToolTarget(t, t + ".txt")).ToList(),
            "snap", 1, "sbx", ContractTime.ToContract(now), ContractTime.ToContract(now.AddMinutes(5)));
        return new SandboxJob(1, "job-1", 5, "s", "inv-1", proposal, ProposalHashing.Sha256(proposal),
            ContractTime.ToContract(now), ContractTime.ToContract(now.AddMinutes(5)));
    }

    private static string Reply(SandboxJob job, string jobId, string proposalId, bool untrusted, params TargetResult[] results) =>
        ContractJson.Serialize(new ResultEnvelope(jobId, job.Sequence, new ToolResult("inv-1", proposalId, ResultStatus.Succeeded, "snap",
            results, "h", false, untrusted)));

    [Fact, Trait("Pruefung", "SEC-18")]
    public void Ergebnis_wird_streng_geprueft()
    {
        var job = Job("t1", "t2");
        var ok = new TargetResult("t1", ResultStatus.Succeeded, Text: "inhalt");

        var good = ResultValidator.Validate(new SandboxReply(Reply(job, "job-1", "p1", false, ok), null), job);
        Assert.Equal(ResultStatus.Succeeded, good.Status);
        Assert.True(good.Untrusted, "Ergebnis immer als untrusted markiert");

        Assert.Equal(ResultStatus.Failed, ResultValidator.Validate(new SandboxReply(Reply(job, "anderer-job", "p1", true, ok), null), job).Status);
        Assert.Equal(ResultStatus.Failed, ResultValidator.Validate(new SandboxReply(Reply(job, "job-1", "fremd", true, ok), null), job).Status);
        Assert.Equal(ResultStatus.Failed, ResultValidator.Validate(new SandboxReply(
            Reply(job, "job-1", "p1", true, new TargetResult("t9", ResultStatus.Succeeded, Text: "x")), null), job).Status);
        Assert.Equal(ResultStatus.Failed, ResultValidator.Validate(new SandboxReply(
            Reply(job, "job-1", "p1", true, ok, ok), null), job).Status);
        Assert.Equal(ResultStatus.Failed, ResultValidator.Validate(new SandboxReply("{kein json", null), job).Status);
        Assert.Equal(ResultStatus.Failed, ResultValidator.Validate(new SandboxReply(null, "Signatur ungültig"), job).Status);
        Assert.Equal(ResultStatus.Failed, ResultValidator.Validate(new SandboxReply(
            Reply(job, "job-1", "p1", true, new TargetResult("t1", ResultStatus.Succeeded, Text: new string('x', ResultValidator.MaxTextChars + 1))), null), job).Status);
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(_dir, recursive: true);
        }
        catch (IOException)
        {
        }
    }
}
