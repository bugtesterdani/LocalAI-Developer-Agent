using System.Text.Json;
using LocalAi.Broker;
using LocalAi.Broker.Host;

// LocalAi.Broker.Host – startet den Approval Broker als eigenen Prozess.
// Gibt beim Start genau eine JSON-Zeile auf stdout aus (Pipe-Name und Rollen-Tokens) und läuft,
// bis stdin geschlossen wird (Elternprozess beendet) oder Strg+C gedrückt wird.

// Ausgaben immer als UTF-8 (ohne BOM), unabhängig von der Konsolen-Codepage (z. B. 850 unter deutschem Windows).
// Aufrufer (Oberfläche, Tests) lesen stdout/stderr als UTF-8.
var utf8 = new System.Text.UTF8Encoding(encoderShouldEmitUTF8Identifier: false);
Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), utf8) { AutoFlush = true });
Console.SetError(new StreamWriter(Console.OpenStandardError(), utf8) { AutoFlush = true });

var options = HostOptions.Parse(args);
Directory.CreateDirectory(options.DataDirectory);

var adapter = new CountingTestAdapter { Delay = options.TestAdapterDelay };
var journal = new AuditJournal(Path.Combine(options.DataDirectory, "audit.jsonl"), SystemClock.Instance);
var brokerOptions = new BrokerOptions { ProposalTtl = options.ProposalTtl };
var broker = new ApprovalBroker(brokerOptions, adapter, journal);

try
{
    broker.Start();
}
catch (AuditTamperException ex)
{
    Console.Error.WriteLine($"FEHLER: Audit-Journal ist beschädigt oder manipuliert: {ex.Message}");
    Console.Error.WriteLine("Der Broker startet nicht. Journal sichern und prüfen, dann umbenennen, um neu zu beginnen.");
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

var server = new PipeServer(pipeName, tokens, broker, adapter, options);
var serverTask = server.RunAsync(shutdown.Token);

var handshake = new
{
    pipe = pipeName,
    sessionId = broker.SessionId,
    uiToken = tokens.Ui,
    orchestratorToken = tokens.Orchestrator,
    dataDirectory = Path.GetFullPath(options.DataDirectory),
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
    await broker.DisposeAsync();
}
return 0;
