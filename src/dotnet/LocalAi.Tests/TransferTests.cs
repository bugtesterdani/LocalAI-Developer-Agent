using System.Buffers.Binary;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>Rückweg VM → Host: VHDX ohne Einbinden lesen, Container streng prüfen (SEC-18).</summary>
public sealed class TransferTests : IDisposable
{
    private const long Max = 64L * 1024 * 1024;
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-xfer-").FullName;

    public void Dispose() => Directory.Delete(_dir, recursive: true);

    private static JsonElement Vector() =>
        JsonDocument.Parse(File.ReadAllText(Path.Combine(ContractTests.RepoRoot(), "contracts", "testvectors", "transfer.json"))).RootElement.Clone();

    private string Disk(byte[] content, bool fixedDisk = true, long size = 4 * 1024 * 1024)
    {
        var path = Path.Combine(_dir, Guid.NewGuid().ToString("N") + ".vhdx");
        VhdxTestWriter.Create(path, new VhdxTestWriter.Options(size, Fixed: fixedDisk), content);
        return path;
    }

    [Fact, Trait("Pruefung", "B01")]
    public void Container_entspricht_Python_Vektor()
    {
        var v = Vector();
        var image = TransferContainer.Build((TransferKind)v.GetProperty("kind").GetInt32(), v.GetProperty("sequence").GetInt64(),
            Encoding.UTF8.GetBytes(v.GetProperty("payloadUtf8").GetString()!));
        Assert.Equal(v.GetProperty("headerHex").GetString(), Convert.ToHexStringLower(image.AsSpan(0, 64)));
        Assert.Equal(v.GetProperty("imageLength").GetInt32(), image.Length);
        Assert.Equal(v.GetProperty("imageSha256").GetString(), Convert.ToHexStringLower(SHA256.HashData(image)));
    }

    [Fact, Trait("Pruefung", "SEC-18")]
    public void Von_qemu_erzeugte_VHDX_wird_gelesen()
    {
        var v = Vector();
        var path = Path.Combine(_dir, "qemu.vhdx");
        using (var gz = new GZipStream(File.OpenRead(Path.Combine(ContractTests.RepoRoot(), "contracts", "testvectors",
                   v.GetProperty("qemuVhdx").GetProperty("file").GetString()!)), CompressionMode.Decompress))
        using (var output = File.Create(path))
        {
            gz.CopyTo(output);
        }
        var message = TransferContainer.Read(path, Max);
        Assert.NotNull(message);
        Assert.Equal(TransferKind.Result, message.Kind);
        Assert.Equal(42, message.Sequence);
        Assert.Equal(v.GetProperty("payloadUtf8").GetString(), Encoding.UTF8.GetString(message.Payload));
    }

    [Theory, Trait("Pruefung", "SEC-18")]
    [InlineData(true)]
    [InlineData(false)]
    public void Feste_und_dynamische_VHDX_liefern_Inhalt(bool fixedDisk)
    {
        var payload = new byte[1_500_000];
        new Random(5).NextBytes(payload);
        var path = Disk(TransferContainer.Build(TransferKind.Report, 0, payload), fixedDisk);
        var message = TransferContainer.Read(path, Max)!;
        Assert.Equal(TransferKind.Report, message.Kind);
        Assert.Equal(payload, message.Payload);
    }

    [Fact, Trait("Pruefung", "SEC-18")]
    public void Leeres_Laufwerk_bedeutet_keine_Antwort() =>
        Assert.Null(TransferContainer.Read(Disk([]), Max));

    [Fact, Trait("Pruefung", "SEC-18")]
    public void Container_Manipulationen_werden_erkannt()
    {
        var good = TransferContainer.Build(TransferKind.Result, 3, "{}"u8);

        var wrongHash = (byte[])good.Clone();
        wrongHash[TransferContainer.HeaderSize] ^= 1;
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(Disk(wrongHash), Max));

        var tooLong = (byte[])good.Clone();
        BinaryPrimitives.WriteUInt64LittleEndian(tooLong.AsSpan(24), (ulong)TransferContainer.MaxPayloadBytes + 1);
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(Disk(tooLong), Max));

        var beyondDisk = (byte[])good.Clone();
        BinaryPrimitives.WriteUInt64LittleEndian(beyondDisk.AsSpan(24), 4 * 1024 * 1024);
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(Disk(beyondDisk), Max));

        var wrongKind = (byte[])good.Clone();
        BinaryPrimitives.WriteUInt32LittleEndian(wrongKind.AsSpan(12), 9);
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(Disk(wrongKind), Max));

        var garbage = (byte[])good.Clone();
        garbage[0] = (byte)'X';
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(Disk(garbage), Max));
    }

    [Fact, Trait("Pruefung", "SEC-18")]
    public void VHDX_Strukturfehler_werden_abgelehnt()
    {
        var content = TransferContainer.Build(TransferKind.Result, 1, "{}"u8);

        // Zu großes Laufwerk.
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(Disk(content), 1024 * 1024));

        // Eine beschädigte Header-Kopie ist erlaubt, zwei nicht.
        var path = Disk(content);
        Patch(path, 64 * 1024 + 100, 0xFF);
        Assert.NotNull(TransferContainer.Read(path, Max));
        Patch(path, 128 * 1024 + 100, 0xFF);
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(path, Max));

        // Offenes Log (mit korrekter Prüfsumme) wird nicht verarbeitet.
        path = Disk(content);
        foreach (var header in new[] { 64 * 1024, 128 * 1024 })
        {
            RewriteHeader(path, header, h => Guid.NewGuid().TryWriteBytes(h.AsSpan(48)));
        }
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(path, Max));

        // BAT zeigt hinter das Dateiende.
        path = Disk(content);
        var entry = 6UL | (999UL << 20);
        using (var f = new FileStream(path, FileMode.Open, FileAccess.Write))
        {
            f.Position = 3 * VhdxReader.Mib;
            var buffer = new byte[8];
            BinaryPrimitives.WriteUInt64LittleEndian(buffer, entry);
            f.Write(buffer);
        }
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(path, Max));

        // Keine VHDX.
        path = Path.Combine(_dir, "kein.vhdx");
        File.WriteAllBytes(path, new byte[8 * 1024 * 1024]);
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(path, Max));
    }

    [Fact]
    public void Crc32C_Pruefwert() =>
        Assert.Equal(0xE3069283u, Crc32C.Compute("123456789"u8));

    private static void Patch(string path, long offset, byte value)
    {
        using var f = new FileStream(path, FileMode.Open, FileAccess.Write);
        f.Position = offset;
        f.WriteByte(value);
    }

    private static void RewriteHeader(string path, long offset, Action<byte[]> change)
    {
        using var f = new FileStream(path, FileMode.Open, FileAccess.ReadWrite);
        var h = new byte[4096];
        f.Position = offset;
        f.ReadExactly(h);
        change(h);
        h.AsSpan(4, 4).Clear();
        BinaryPrimitives.WriteUInt32LittleEndian(h.AsSpan(4), Crc32C.Compute(h));
        f.Position = offset;
        f.Write(h);
    }
}
