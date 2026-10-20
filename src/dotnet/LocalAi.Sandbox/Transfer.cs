using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;

namespace LocalAi.Sandbox;

/// <summary>CRC-32C (Castagnoli), wie von VHDX für Header und Regionstabellen verlangt.</summary>
public static class Crc32C
{
    private static readonly uint[] Table = BuildTable();

    public static uint Compute(ReadOnlySpan<byte> data)
    {
        var crc = 0xFFFFFFFFu;
        foreach (var b in data)
        {
            crc = Table[(crc ^ b) & 0xFF] ^ (crc >> 8);
        }
        return crc ^ 0xFFFFFFFFu;
    }

    private static uint[] BuildTable()
    {
        var table = new uint[256];
        for (uint i = 0; i < 256; i++)
        {
            var c = i;
            for (var k = 0; k < 8; k++)
            {
                c = (c & 1) != 0 ? 0x82F63B78u ^ (c >> 1) : c >> 1;
            }
            table[i] = c;
        }
        return table;
    }
}

public sealed class VhdxFormatException(string message) : Exception(message);

/// <summary>Lesezugriff auf ein vom Gast beschriebenes Laufwerk (VHDX unter Hyper-V, Rohformat unter QEMU).</summary>
public interface IVirtualDiskReader : IDisposable
{
    long VirtualSize { get; }

    void ReadAt(long position, Span<byte> buffer);
}

/// <summary>
/// Transfer-Laufwerk im Rohformat (QEMU, M10): die Datei ist das Laufwerk. Größer als erlaubt → Ablehnung;
/// hinter dem Dateiende liest es sich als Nullen (Datei kann sparse sein).
/// </summary>
public sealed class RawDiskReader : IVirtualDiskReader
{
    private readonly FileStream _file;

    private RawDiskReader(FileStream file) => _file = file;

    public long VirtualSize => _file.Length;

    public static RawDiskReader Open(string path, long maxVirtualSize)
    {
        var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 4096, FileOptions.RandomAccess);
        var length = file.Length;
        if (length > maxVirtualSize)
        {
            file.Dispose();
            throw new VhdxFormatException($"Transfer-Laufwerk ist größer als erlaubt ({length} Bytes)");
        }
        return new RawDiskReader(file);
    }

    public void ReadAt(long position, Span<byte> buffer)
    {
        buffer.Clear();
        var done = 0;
        while (done < buffer.Length && position + done < _file.Length)
        {
            var read = RandomAccess.Read(_file.SafeFileHandle, buffer[done..], position + done);
            if (read <= 0)
            {
                break;
            }
            done += read;
        }
    }

    public void Dispose() => _file.Dispose();
}

/// <summary>
/// Liest den Inhalt einer VHDX-Datei direkt aus der Datei – ohne Einbinden ins Host-Betriebssystem.
/// Die Transfer-VHDX wird vom Gast beschrieben und ist deshalb nicht vertrauenswürdig: jede Struktur wird
/// gegen Dateigröße, Ausrichtung und Prüfsummen geprüft; nicht benötigte Funktionen (Log, Elternlaufwerk)
/// führen zur Ablehnung. Unterstützt feste und dynamische VHDX (fehlende Blöcke lesen sich als Nullen).
/// </summary>
public sealed class VhdxReader : IVirtualDiskReader
{
    public const long Mib = 1024 * 1024;
    private static readonly Guid BatRegion = new("2DC27766-F623-4200-9D64-115E9BFD4A08");
    private static readonly Guid MetadataRegion = new("8B7CA206-4790-4B9A-B8FE-575F050F886E");
    private static readonly Guid FileParameters = new("CAA16737-FA36-4D43-B3B6-33F0AA44E76B");
    private static readonly Guid VirtualDiskSizeItem = new("2FA54224-CD1B-4876-B211-5DBED83BF4B8");
    private static readonly Guid LogicalSectorSizeItem = new("8141BF1D-A96F-4709-BA47-F233A8FAAB5F");

    private readonly FileStream _file;
    private readonly ulong[] _bat;
    private readonly long _chunkRatio;

    private VhdxReader(FileStream file, long virtualSize, int blockSize, int logicalSectorSize, ulong[] bat, long chunkRatio)
    {
        _file = file;
        VirtualSize = virtualSize;
        BlockSize = blockSize;
        LogicalSectorSize = logicalSectorSize;
        _bat = bat;
        _chunkRatio = chunkRatio;
    }

    public long VirtualSize { get; }

    public int BlockSize { get; }

    public int LogicalSectorSize { get; }

    /// <summary>Öffnet eine VHDX nur lesend. <paramref name="maxVirtualSize"/> begrenzt die akzeptierte Laufwerksgröße.</summary>
    public static VhdxReader Open(string path, long maxVirtualSize)
    {
        var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 4096, FileOptions.RandomAccess);
        try
        {
            return Parse(file, maxVirtualSize);
        }
        catch
        {
            file.Dispose();
            throw;
        }
    }

    private static VhdxReader Parse(FileStream file, long maxVirtualSize)
    {
        var length = file.Length;
        if (length < 4 * Mib)
        {
            throw new VhdxFormatException("Datei zu klein für eine VHDX");
        }
        if (Encoding.ASCII.GetString(ReadExact(file, 0, 8)) != "vhdxfile")
        {
            throw new VhdxFormatException("Keine VHDX-Datei (Kennung fehlt)");
        }

        // Zwei Header-Kopien: die gültige mit der höheren Sequenznummer gilt.
        byte[]? header = null;
        ulong bestSequence = 0;
        foreach (var offset in new long[] { 64 * 1024, 128 * 1024 })
        {
            var h = ReadExact(file, offset, 4096);
            if (Encoding.ASCII.GetString(h, 0, 4) != "head" || !ChecksumOk(h, 4))
            {
                continue;
            }
            var sequence = BinaryPrimitives.ReadUInt64LittleEndian(h.AsSpan(8));
            if (header is null || sequence > bestSequence)
            {
                header = h;
                bestSequence = sequence;
            }
        }
        if (header is null)
        {
            throw new VhdxFormatException("Kein gültiger VHDX-Header");
        }
        if (new Guid(header.AsSpan(48, 16)) != Guid.Empty)
        {
            throw new VhdxFormatException("VHDX enthält ein offenes Protokoll (Log) – wird nicht verarbeitet");
        }
        if (BinaryPrimitives.ReadUInt16LittleEndian(header.AsSpan(66)) != 1)
        {
            throw new VhdxFormatException("Unbekannte VHDX-Version");
        }

        // Regionstabelle (zwei Kopien, die erste gültige gilt).
        byte[]? regions = null;
        foreach (var offset in new long[] { 192 * 1024, 256 * 1024 })
        {
            var r = ReadExact(file, offset, 64 * 1024);
            if (Encoding.ASCII.GetString(r, 0, 4) == "regi" && ChecksumOk(r, 4))
            {
                regions = r;
                break;
            }
        }
        if (regions is null)
        {
            throw new VhdxFormatException("Keine gültige Regionstabelle");
        }
        var entryCount = BinaryPrimitives.ReadUInt32LittleEndian(regions.AsSpan(8));
        if (entryCount > 2047)
        {
            throw new VhdxFormatException("Regionstabelle zu groß");
        }
        (long Offset, long Length)? bat = null, meta = null;
        for (var i = 0; i < entryCount; i++)
        {
            var e = regions.AsSpan(16 + i * 32, 32);
            var id = new Guid(e[..16]);
            var offset = (long)BinaryPrimitives.ReadUInt64LittleEndian(e[16..]);
            var len = (long)BinaryPrimitives.ReadUInt32LittleEndian(e[24..]);
            var required = (BinaryPrimitives.ReadUInt32LittleEndian(e[28..]) & 1) != 0;
            CheckRegion(offset, len, length);
            if (id == BatRegion)
            {
                bat = (offset, len);
            }
            else if (id == MetadataRegion)
            {
                meta = (offset, len);
            }
            else if (required)
            {
                throw new VhdxFormatException($"Unbekannte Pflichtregion {id}");
            }
        }
        if (bat is null || meta is null)
        {
            throw new VhdxFormatException("BAT oder Metadaten fehlen");
        }

        // Metadaten: Blockgröße, Laufwerksgröße, Sektorgröße.
        var metaTable = ReadExact(file, meta.Value.Offset, 64 * 1024);
        if (Encoding.ASCII.GetString(metaTable, 0, 8) != "metadata")
        {
            throw new VhdxFormatException("Metadatentabelle ungültig");
        }
        var metaCount = BinaryPrimitives.ReadUInt16LittleEndian(metaTable.AsSpan(10));
        if (metaCount > 2047)
        {
            throw new VhdxFormatException("Metadatentabelle zu groß");
        }
        byte[]? ReadItem(Guid id, int size)
        {
            for (var i = 0; i < metaCount; i++)
            {
                var e = metaTable.AsSpan(32 + i * 32, 32);
                if (new Guid(e[..16]) != id)
                {
                    continue;
                }
                var off = BinaryPrimitives.ReadUInt32LittleEndian(e[16..]);
                var len = BinaryPrimitives.ReadUInt32LittleEndian(e[20..]);
                if (len < size || off < 64 * 1024 || off + (long)len > meta.Value.Length)
                {
                    throw new VhdxFormatException($"Metadatum {id} liegt außerhalb der Region");
                }
                return ReadExact(file, meta.Value.Offset + off, size);
            }
            return null;
        }
        var parameters = ReadItem(FileParameters, 8) ?? throw new VhdxFormatException("Dateiparameter fehlen");
        var blockSize = BinaryPrimitives.ReadUInt32LittleEndian(parameters);
        var hasParent = (BinaryPrimitives.ReadUInt32LittleEndian(parameters.AsSpan(4)) & 2) != 0;
        if (hasParent)
        {
            throw new VhdxFormatException("Differenzierende VHDX wird nicht unterstützt");
        }
        if (blockSize < Mib || blockSize > 256 * Mib || (blockSize & (blockSize - 1)) != 0)
        {
            throw new VhdxFormatException("Blockgröße ungültig");
        }
        var virtualSize = (long)BinaryPrimitives.ReadUInt64LittleEndian(
            ReadItem(VirtualDiskSizeItem, 8) ?? throw new VhdxFormatException("Laufwerksgröße fehlt"));
        var sectorSize = (int)BinaryPrimitives.ReadUInt32LittleEndian(
            ReadItem(LogicalSectorSizeItem, 4) ?? throw new VhdxFormatException("Sektorgröße fehlt"));
        if (sectorSize is not (512 or 4096))
        {
            throw new VhdxFormatException("Sektorgröße ungültig");
        }
        if (virtualSize <= 0 || virtualSize > maxVirtualSize || virtualSize % sectorSize != 0)
        {
            throw new VhdxFormatException($"Laufwerksgröße {virtualSize} nicht zulässig (höchstens {maxVirtualSize})");
        }

        // BAT: Payload-Block i steht an Index i + i / chunkRatio (dazwischen Sektor-Bitmaps).
        var chunkRatio = (1L << 23) * sectorSize / blockSize;
        var dataBlocks = (virtualSize + blockSize - 1) / blockSize;
        var entries = dataBlocks + (dataBlocks - 1) / chunkRatio;
        if (entries * 8 > bat.Value.Length)
        {
            throw new VhdxFormatException("BAT zu klein");
        }
        var raw = ReadExact(file, bat.Value.Offset, checked((int)(entries * 8)));
        var table = new ulong[entries];
        for (var i = 0; i < entries; i++)
        {
            table[i] = BinaryPrimitives.ReadUInt64LittleEndian(raw.AsSpan(i * 8));
        }
        return new VhdxReader(file, virtualSize, (int)blockSize, sectorSize, table, chunkRatio);
    }

    /// <summary>Liest <paramref name="buffer"/>.Length Bytes ab der virtuellen Position.</summary>
    public void ReadAt(long position, Span<byte> buffer)
    {
        if (position < 0 || position + buffer.Length > VirtualSize)
        {
            throw new VhdxFormatException("Lesebereich außerhalb des Laufwerks");
        }
        while (!buffer.IsEmpty)
        {
            var block = position / BlockSize;
            var inBlock = (int)(position % BlockSize);
            var count = Math.Min(buffer.Length, BlockSize - inBlock);
            var entry = _bat[block + block / _chunkRatio];
            var state = entry & 7;
            switch (state)
            {
                case 0 or 1 or 2 or 3:
                    buffer[..count].Clear();
                    break;
                case 6:
                {
                    var fileOffset = (long)(entry >> 20) * Mib;
                    if (fileOffset < Mib || fileOffset + BlockSize > _file.Length)
                    {
                        throw new VhdxFormatException("Datenblock außerhalb der Datei");
                    }
                    _file.Position = fileOffset + inBlock;
                    _file.ReadExactly(buffer[..count]);
                    break;
                }
                default:
                    throw new VhdxFormatException($"Blockzustand {state} nicht unterstützt");
            }
            buffer = buffer[count..];
            position += count;
        }
    }

    public void Dispose() => _file.Dispose();

    private static void CheckRegion(long offset, long length, long fileLength)
    {
        if (offset < Mib || offset % Mib != 0 || length <= 0 || length % Mib != 0 || offset + length > fileLength)
        {
            throw new VhdxFormatException("Region außerhalb der Datei oder falsch ausgerichtet");
        }
    }

    private static bool ChecksumOk(byte[] data, int checksumOffset)
    {
        var expected = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(checksumOffset));
        var copy = (byte[])data.Clone();
        copy.AsSpan(checksumOffset, 4).Clear();
        return Crc32C.Compute(copy) == expected;
    }

    private static byte[] ReadExact(FileStream file, long offset, int count)
    {
        if (offset + count > file.Length)
        {
            throw new VhdxFormatException("Struktur außerhalb der Datei");
        }
        var buffer = new byte[count];
        file.Position = offset;
        file.ReadExactly(buffer);
        return buffer;
    }
}

public enum TransferKind : uint
{
    Result = 1,
    Rejected = 2,
    Report = 3,
}

public sealed record TransferMessage(TransferKind Kind, long Sequence, byte[] Payload);

/// <summary>
/// Format der Rückgabe auf der Transfer-VHDX: kein Dateisystem, sondern ein Container direkt ab Sektor 0
/// (identisch in Python: <c>localai.worker.transfer</c>).
/// <code>
///  0  8  "LAIXFER1"
///  8  4  Version = 1          (u32 LE)
/// 12  4  Art 1/2/3            (u32 LE: Ergebnis, abgelehnt, Bericht)
/// 16  8  Sequenz              (u64 LE, Bericht: 0)
/// 24  8  Nutzlastlänge        (u64 LE)
/// 32 32  SHA-256 der Nutzlast
/// 4096   Nutzlast (UTF-8-JSON bzw. Text), mit Nullen auf 4096 aufgefüllt
/// </code>
/// </summary>
public static class TransferContainer
{
    public const int HeaderSize = 4096;
    public const long MaxPayloadBytes = 16L * 1024 * 1024;
    private static readonly byte[] Magic = "LAIXFER1"u8.ToArray();

    /// <summary>Erzeugt das Abbild (für Tests und Gegenproben; im Betrieb schreibt nur der Gast).</summary>
    public static byte[] Build(TransferKind kind, long sequence, ReadOnlySpan<byte> payload)
    {
        var padded = (payload.Length + HeaderSize - 1) / HeaderSize * HeaderSize;
        var image = new byte[HeaderSize + padded];
        Magic.CopyTo(image, 0);
        BinaryPrimitives.WriteUInt32LittleEndian(image.AsSpan(8), 1);
        BinaryPrimitives.WriteUInt32LittleEndian(image.AsSpan(12), (uint)kind);
        BinaryPrimitives.WriteUInt64LittleEndian(image.AsSpan(16), (ulong)sequence);
        BinaryPrimitives.WriteUInt64LittleEndian(image.AsSpan(24), (ulong)payload.Length);
        SHA256.HashData(payload).CopyTo(image, 32);
        payload.CopyTo(image.AsSpan(HeaderSize));
        return image;
    }

    /// <summary>
    /// Liest den Container aus einem vom Gast zurückgegebenen Laufwerk (.vhdx unter Hyper-V, .raw unter QEMU).
    /// <c>null</c>: Laufwerk ist leer.
    /// </summary>
    public static TransferMessage? Read(string diskPath, long maxDiskBytes)
    {
        using IVirtualDiskReader disk = diskPath.EndsWith(".raw", StringComparison.OrdinalIgnoreCase)
            ? RawDiskReader.Open(diskPath, maxDiskBytes)
            : VhdxReader.Open(diskPath, maxDiskBytes);
        var header = new byte[HeaderSize];
        disk.ReadAt(0, header);
        if (header.AsSpan(0, 8).IndexOfAnyExcept((byte)0) < 0)
        {
            return null;
        }
        if (!header.AsSpan(0, 8).SequenceEqual(Magic))
        {
            throw new VhdxFormatException("Transfer-Laufwerk enthält keinen gültigen Container");
        }
        if (BinaryPrimitives.ReadUInt32LittleEndian(header.AsSpan(8)) != 1)
        {
            throw new VhdxFormatException("Unbekannte Containerversion");
        }
        var kind = BinaryPrimitives.ReadUInt32LittleEndian(header.AsSpan(12));
        if (kind is < 1 or > 3)
        {
            throw new VhdxFormatException($"Unbekannte Containerart {kind}");
        }
        var sequence = BinaryPrimitives.ReadUInt64LittleEndian(header.AsSpan(16));
        var length = BinaryPrimitives.ReadUInt64LittleEndian(header.AsSpan(24));
        if (length > (ulong)MaxPayloadBytes || (long)length > disk.VirtualSize - HeaderSize || sequence > long.MaxValue)
        {
            throw new VhdxFormatException($"Nutzlast zu groß ({length} Bytes)");
        }
        var payload = new byte[(int)length];
        disk.ReadAt(HeaderSize, payload);
        if (!CryptographicOperations.FixedTimeEquals(SHA256.HashData(payload), header.AsSpan(32, 32)))
        {
            throw new VhdxFormatException("Prüfsumme der Nutzlast stimmt nicht");
        }
        return new TransferMessage((TransferKind)kind, (long)sequence, payload);
    }
}
