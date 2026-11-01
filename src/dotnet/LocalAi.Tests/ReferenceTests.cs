using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>Nachschlageordner (references): schreibgeschützte Grants ref-&lt;id&gt;, Reference.Search/Read nur dort.</summary>
public sealed class ReferenceTests : IDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-ref-").FullName;

    public void Dispose() => FileTools.DeleteTree(_dir);

    private ProjectConfig Load(string json)
    {
        Directory.CreateDirectory(Path.Combine(_dir, "p"));
        Directory.CreateDirectory(Path.Combine(_dir, "hilfe"));
        var path = Path.Combine(_dir, "p.json");
        File.WriteAllText(path, json);
        return ProjectLoader.Load(path, _dir);
    }

    private const string Project = """
        {"schemaVersion":1,"projectId":"p","name":"P",
         "grants":[{"grantId":"g","root":"p","rights":["List","Read","Modify"],"recursive":true}],
         "references":[{"referenceId":"win32","root":"hilfe","title":"Win32-Hilfe","exclude":["alt"]}]}
        """;

    [Fact, Trait("Pruefung", "Referenz")]
    public void Projektdatei_fuehrt_Nachschlageordner_als_schreibgeschuetzte_Grants()
    {
        var project = Load(Project);
        Assert.Equal(["g", "ref-win32"], project.Grants.Select(g => g.GrantId));
        var reference = project.Grants[1];
        Assert.Equal([FolderRight.List, FolderRight.Read], reference.Rights);
        Assert.True(reference.Reference);
        Assert.True(reference.Recursive);
        Assert.Equal("Win32-Hilfe", reference.Title);
        Assert.Equal(["alt"], reference.Exclude!);
        Assert.Equal(Path.Combine(_dir, "hilfe"), reference.Root);
    }

    [Theory, Trait("Pruefung", "Referenz")]
    [InlineData("""{"referenceId":"Win 32","root":"hilfe"}""", "ungültige referenceId")]
    [InlineData("""{"referenceId":"x","root":"fehlt"}""", "existiert nicht")]
    public void Ungueltige_Nachschlageordner_verhindern_den_Start(string entry, string expected)
    {
        var ex = Assert.ThrowsAny<Exception>(() => Load($$"""
            {"schemaVersion":1,"projectId":"p","name":"P","grants":[{"grantId":"g","root":"p","rights":["Read"],"recursive":true}],
             "references":[{{entry}}]}
            """));
        Assert.Contains(expected, ex.Message, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "Referenz")]
    public void Grants_duerfen_sich_nicht_als_Nachschlageordner_ausgeben()
    {
        Assert.Throws<InvalidDataException>(() => Load("""
            {"schemaVersion":1,"projectId":"p","name":"P","grants":[{"grantId":"g","root":"p","rights":["Read","Modify"],"recursive":true,"reference":true}]}
            """));
        Assert.Throws<InvalidDataException>(() => Load("""
            {"schemaVersion":1,"projectId":"p","name":"P","grants":[{"grantId":"ref-x","root":"p","rights":["Read"],"recursive":true}]}
            """));
        Assert.Throws<InvalidDataException>(() => Load("""
            {"schemaVersion":1,"projectId":"p","name":"P","grants":[{"grantId":"g","root":"p","rights":["Read"],"recursive":true}],
             "references":[{"referenceId":"a","root":"hilfe"},{"referenceId":"a","root":"hilfe"}]}
            """));
    }

    private static ProposalRequest Request(string tool, object args, params string[] targets) =>
        new(1, "run", "T1", AgentRole.Coder, tool, "1", "Nachschlagen", JsonSerializer.SerializeToElement(args),
            targets.Select((t, i) => new ToolTarget($"t{i + 1}", t)).ToList(), "snap", Guid.NewGuid().ToString("N"));

    private static int Code(Action action) => Assert.Throws<BrokerException>(action).Code;

    [Fact, Trait("Pruefung", "Referenz")]
    public void Reference_Tools_nur_fuer_Nachschlageordner_und_nur_lesend()
    {
        var project = Load(Project);
        var registry = new SnapshotRegistry();
        registry.Add("snap", "ref-win32");
        var policy = new ProposalPolicy(new BrokerOptions(), new ProjectPolicyContext(project, registry));

        policy.Validate(Request("Reference.Search", new { grantId = "ref-win32", query = "CreateFile", maxResults = 10 }));
        policy.Validate(Request("Reference.Read", new { grantId = "ref-win32", maxChars = 12000, part = 2 }, "api.chm/html/x.htm"));
        // Nicht im Projektordner, nicht ohne Seite, keine unsinnigen Grenzen.
        registry.Add("snapg", "g");
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => policy.Validate(Request("Reference.Search", new { grantId = "g", query = "x" }) with { SnapshotId = "snapg" })));
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => policy.Validate(Request("Reference.Read", new { grantId = "ref-win32" }))));
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => policy.Validate(Request("Reference.Search", new { grantId = "ref-win32", query = "" }))));
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => policy.Validate(Request("Reference.Search", new { grantId = "ref-win32", query = "x", maxResults = 500 }))));
        Assert.Equal(BrokerErrorCodes.Unprocessable, Code(() => policy.Validate(Request("Reference.Read", new { grantId = "ref-win32", part = 0 }, "a.htm"))));
        // Ändern ist im Nachschlageordner nie möglich (nur List/Read).
        var patch = "--- a/a.htm\n+++ b/a.htm\n@@ -1 +1 @@\n-a\n+b\n";
        Assert.Equal(BrokerErrorCodes.Forbidden, Code(() => policy.Validate(Request("Patch.Apply", new { grantId = "ref-win32", patch }, "a.htm") with { Agent = AgentRole.Coder })));
        Assert.Contains("Reference.Search", AutoApprove.Eligible.Keys);
    }
}
