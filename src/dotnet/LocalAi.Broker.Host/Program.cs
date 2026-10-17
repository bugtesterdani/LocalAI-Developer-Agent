using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Broker.Host;
using LocalAi.Contracts;
using LocalAi.Sandbox;

// LocalAi.Broker.Host – startet den Approval Broker als eigenen Prozess.
// Gibt beim Start genau eine JSON-Zeile auf stdout aus (Pipe-Name, Rollen-Tokens, Betriebsart) und läuft,
// bis stdin geschlossen wird (Elternprozess beendet) oder Strg+C gedrückt wird.

// Ausgaben immer als UTF-8 (ohne BOM), unabhängig von der Konsolen-Codepage (z. B. 850 unter deutschem Windows).
var utf8 = new System.Text.UTF8Encoding(encoderShouldEmitUTF8Identifier: false);
Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), utf8) { AutoFlush = true });
Console.SetError(new StreamWriter(Console.OpenStandardError(), utf8) { AutoFlush = true });

HostOptions options;
try
{
    options = HostOptions.Parse(args);
}
catch (Exception ex) when (ex is ArgumentException or JsonException or IOException)
{
    Console.Error.WriteLine($"FEHLER: Konfiguration ungültig: {ex.Message}");
    return 2;
}
Directory.CreateDirectory(options.DataDirectory);

var children = new ChildProcesses();
var clock = SystemClock.Instance;
CountingTestAdapter? testAdapter;
IToolAdapter adapter;
IPolicyContext policy;
HyperVSandbox? vm;
ToolchainRegistry? toolchains = null;
try
{
    var project = options.Mode == SandboxMode.Test ? null : LoadProject(options);
    if (project is not null)
    {
        toolchains = new ToolchainRegistry(Path.Combine(options.DataDirectory, "toolchains.json"), options.ToolchainsDirectory,
            project.Toolchains ?? [], clock);
    }
    (adapter, policy, testAdapter, vm) = CreateSandbox(options, children, Guid.NewGuid().ToString("D"), project, toolchains);
}
catch (Exception ex) when (ex is IOException or InvalidDataException or JsonException or ChildProcessException or NotSupportedException)
{
    Console.Error.WriteLine($"FEHLER: Sandbox/Projekt konnte nicht eingerichtet werden: {ex.Message}");
    children.StopAll();
    return 4;
}

var journal = new AuditJournal(Path.Combine(options.DataDirectory, "audit.db"), clock);
var brokerOptions = new BrokerOptions { ProposalTtl = options.ProposalTtl, SandboxId = $"sbx-{HostOptions.ModeName(options.Mode)}" };
var broker = new ApprovalBroker(brokerOptions, adapter, journal, clock, policy);

try
{
    broker.Start();
}
catch (AuditTamperException ex)
{
    Console.Error.WriteLine($"FEHLER: Audit-Journal ist beschädigt oder manipuliert: {ex.Message}");
    Console.Error.WriteLine("Der Broker startet nicht. Journal sichern und prüfen, dann umbenennen, um neu zu beginnen.");
    children.StopAll();
    return 3;
}

var tokens = RoleTokens.Create();
var pipeName = options.PipeName ?? $"localai-broker-{Guid.NewGuid():N}";
using var shutdown = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) =>
{
    e.Cancel = true;
    shutdown.Cancel();
};

var server = new PipeServer(pipeName, tokens, broker, testAdapter, children, options)
{
    SandboxStatus = () => vm?.Status.Text,
    Toolchains = toolchains,
};
if (vm is not null)
{
    // Aufbau im Hintergrund; Fortschritt als Ereignis an die Oberfläche und ins Journal.
    vm.StatusChanged += s =>
    {
        try
        {
            broker.PublishInfo("sandbox.status", s.Text);
        }
        catch (ObjectDisposedException)
        {
        }
    };
    vm.Start();
}
var serverTask = server.RunAsync(shutdown.Token);

var handshake = new
{
    pipe = pipeName,
    sessionId = broker.SessionId,
    uiToken = tokens.Ui,
    orchestratorToken = tokens.Orchestrator,
    dataDirectory = Path.GetFullPath(options.DataDirectory),
    mode = HostOptions.ModeName(options.Mode),
};
Console.Out.WriteLine(JsonSerializer.Serialize(handshake));
Console.Out.Flush();

if (!options.IgnoreStdin)
{
    // Lebensdauer an den Elternprozess koppeln: stdin-EOF beendet den Broker.
    _ = Task.Run(async () =>
    {
        try
        {
            using var stdin = Console.OpenStandardInput();
            var buffer = new byte[256];
            while (await stdin.ReadAsync(buffer, shutdown.Token) > 0)
            {
            }
        }
        catch (OperationCanceledException)
        {
        }
        shutdown.Cancel();
    });
}

try
{
    await serverTask;
}
catch (OperationCanceledException)
{
}
finally
{
    children.StopAll();
    if (vm is not null)
    {
        await vm.DisposeAsync();
    }
    await broker.DisposeAsync();
}
return 0;

static (IToolAdapter Adapter, IPolicyContext Policy, CountingTestAdapter? Test, HyperVSandbox? Vm) CreateSandbox(
    HostOptions options, ChildProcesses children, string sessionId, ProjectConfig? loadedProject, ToolchainRegistry? toolchains)
{
    switch (options.Mode)
    {
        case SandboxMode.Test:
        {
            var test = new CountingTestAdapter { Delay = options.TestAdapterDelay };
            return (test, new TestPolicyContext(), test, null);
        }
        case SandboxMode.None:
        {
            var project = loadedProject!;
            return (new NoSandboxAdapter(), new ProjectPolicyContext(project, new SnapshotRegistry()), null, null);
        }
        case SandboxMode.DevLocalUnsafe:
        {
            var project = loadedProject!;
            var registry = new SnapshotRegistry();
            var context = new ProjectPolicyContext(project, registry);
            var session = new SandboxSession(sessionId);
            var dir = Path.Combine(options.DataDirectory, "sandbox-dev");
            // Wie der spätere VM-Neuaufbau: jede Sitzung beginnt mit leerem Arbeitsbereich.
            if (Directory.Exists(dir))
            {
                Directory.Delete(dir, recursive: true);
            }
            var transport = new LocalDirectoryTransport(Path.Combine(dir, "inbox"), Path.Combine(dir, "outbox"));
            var keyFile = Path.Combine(dir, "session.key");
            File.WriteAllText(keyFile, Convert.ToHexStringLower(session.Key));
            var worker = options.DevWorker ?? throw new InvalidDataException("sandbox.devWorker fehlt in config/localai.json");
            children.Start(worker,
                ["--inbox", transport.Inbox, "--outbox", transport.Outbox, "--work", Path.Combine(dir, "work"), "--key-file", keyFile],
                new Dictionary<string, string>(), Path.Combine(options.DataDirectory, "worker.log"));
            var adapter = new SandboxToolAdapter(session, transport, context, registry, Path.Combine(dir, "scratch"), SystemClock.Instance);
            return (adapter, context, null, null);
        }
        case SandboxMode.HyperV:
        {
            var project = loadedProject!;
            var registry = new SnapshotRegistry();
            var context = new ProjectPolicyContext(project, registry);
            var session = new SandboxSession(sessionId);
            var settings = options.HyperV ?? throw new InvalidDataException("sandbox.hyperv fehlt in config/localai.json");
            var host = new PowerShellHyperVHost(settings, Path.Combine(options.RepoRoot, "scripts", "sandbox"),
                Path.Combine(options.DataDirectory, "hyperv.log"));
            var vm = new HyperVSandbox(settings, host, guestDir => Task.Run(() =>
            {
                // Toolchains zuerst bereitstellen und gegen die Registrierung prüfen (SEC-19/SEC-24).
                var staged = toolchains!.Stage(Path.Combine(guestDir, "LocalAi", "toolchain-src"), CancellationToken.None);
                GuestPayloadBuilder.Build(guestDir, GuestPayloadSources.FromRepository(options.RepoRoot), session.Key, settings.TransferDiskBytes, staged);
            }), project.Toolchains ?? []);
            var adapter = new SandboxToolAdapter(session, vm, context, registry, Path.Combine(options.DataDirectory, "sandbox-scratch"), SystemClock.Instance);
            return (adapter, context, null, vm);
        }
        default:
            throw new NotSupportedException($"Betriebsart {options.Mode} wird nicht unterstützt");
    }
}

static ProjectConfig LoadProject(HostOptions options) =>
    options.ProjectPath is null
        ? new ProjectConfig(ContractJson.SchemaVersion, "kein-projekt", "Kein Projekt", [])
        : ProjectLoader.Load(options.ProjectPath, options.RepoRoot);
