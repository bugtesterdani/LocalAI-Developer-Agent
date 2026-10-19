using System.Net.Sockets;

namespace Kontakte.Tests;

/// <summary>
/// Demo (Spezifikation §16): Testcode versucht absichtlich, das Netz zu erreichen – Internet, LAN und den Ollama-Port.
/// In der Offline-VM (ohne Netzwerkkarte) muss jeder Versuch scheitern; dann ist dieser Test grün.
/// </summary>
public sealed class NetzwerkTests
{
    public static TheoryData<string, int> Ziele() => new()
    {
        { "example.com", 443 },
        { "1.1.1.1", 443 },
        { "10.0.0.1", 11434 },
        { "192.168.178.1", 11434 },
    };

    [Theory]
    [MemberData(nameof(Ziele))]
    public async Task Testcode_erreicht_kein_Netz(string host, int port)
    {
        using var client = new TcpClient();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
        var erreicht = false;
        try
        {
            await client.ConnectAsync(host, port, timeout.Token);
            erreicht = true;
        }
        catch (Exception ex) when (ex is SocketException or OperationCanceledException)
        {
        }
        Assert.False(erreicht, $"Verbindung zu {host}:{port} war möglich – die Sandbox ist nicht offline");
    }
}
