using System.Buffers.Binary;
using System.Text;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>
/// Erzeugt minimale VHDX-Dateien für Tests (im Betrieb legt Hyper-V sie mit New-VHD an).
/// Layout: 0 Kennung, 64K/128K Header, 192K/256K Regionstabelle, 1M Log, 2M Metadaten, 3M BAT, danach Blöcke.
/// </summary>
public static class VhdxTestWriter
{
    private const long Mib = VhdxReader.Mib;

    public sealed record Options(long VirtualSize, int BlockSize = 1024 * 1024, int SectorSize = 512, bool Fixed = true);

    public static void Create(string path, Options o, ReadOnlySpan<byte> content = default)
    {
        var chunkRatio = (1L << 23) * o.SectorSize / o.BlockSize;
        var dataBlocks = (o.VirtualSize + o.BlockSize - 1) / o.BlockSize;
        var entries = dataBlocks + (dataBlocks - 1) / chunkRatio;
        var batBytes = (entries * 8 + Mib - 1) / Mib * Mib;
        var dataStart = 3 * Mib + batBytes;
        var fileLength = dataStart + (o.Fixed ? dataBlocks * o.BlockSize : 0);
        // Dynamisch: nur Blöcke mit Inhalt anlegen.
        var present = new bool[dataBlocks];
        for (var b = 0; b < dataBlocks; b++)
        {
            var start = b * (long)o.BlockSize;
            present[b] = o.Fixed || (start < content.Length && content.Slice((int)start, (int)Math.Min(o.BlockSize, content.Length - start)).IndexOfAnyExcept((byte)0) >= 0);
        }
        var blockOffsets = new long[dataBlocks];
        var next = dataStart;
        for (var b = 0; b < dataBlocks; b++)
        {
            if (present[b])
            {
                blockOffsets[b] = next;
                next += o.BlockSize;
            }
        }
        fileLength = next;

        using var f = new FileStream(path, FileMode.Create, FileAccess.ReadWrite);
        f.SetLength(fileLength);
        void Put(long offset, ReadOnlySpan<byte> data)
        {
            f.Position = offset;
            f.Write(data);
        }

        var fti = new byte[64 * 1024];
        Encoding.ASCII.GetBytes("vhdxfile").CopyTo(fti, 0);
        Encoding.Unicode.GetBytes("LocalAi tests").CopyTo(fti, 8);
        Put(0, fti);

        for (var copy = 0; copy < 2; copy++)
        {
            var h = new byte[4096];
            Encoding.ASCII.GetBytes("head").CopyTo(h, 0);
            BinaryPrimitives.WriteUInt64LittleEndian(h.AsSpan(8), (ulong)(copy + 1));
            Guid.NewGuid().TryWriteBytes(h.AsSpan(16));
            Guid.NewGuid().TryWriteBytes(h.AsSpan(32));
            BinaryPrimitives.WriteUInt16LittleEndian(h.AsSpan(64), 0);
            BinaryPrimitives.WriteUInt16LittleEndian(h.AsSpan(66), 1);
            BinaryPrimitives.WriteUInt32LittleEndian(h.AsSpan(68), (uint)Mib);
            BinaryPrimitives.WriteUInt64LittleEndian(h.AsSpan(72), (ulong)Mib);
            BinaryPrimitives.WriteUInt32LittleEndian(h.AsSpan(4), Crc32C.Compute(h));
            Put((copy + 1) * 64 * 1024, h);
        }

        var r = new byte[64 * 1024];
        Encoding.ASCII.GetBytes("regi").CopyTo(r, 0);
        BinaryPrimitives.WriteUInt32LittleEndian(r.AsSpan(8), 2);
        void Region(int i, string guid, long offset, long length)
        {
            var e = r.AsSpan(16 + i * 32);
            new Guid(guid).TryWriteBytes(e);
            BinaryPrimitives.WriteUInt64LittleEndian(e[16..], (ulong)offset);
            BinaryPrimitives.WriteUInt32LittleEndian(e[24..], (uint)length);
            BinaryPrimitives.WriteUInt32LittleEndian(e[28..], 1);
        }
        Region(0, "2DC27766-F623-4200-9D64-115E9BFD4A08", 3 * Mib, batBytes);
        Region(1, "8B7CA206-4790-4B9A-B8FE-575F050F886E", 2 * Mib, Mib);
        BinaryPrimitives.WriteUInt32LittleEndian(r.AsSpan(4), Crc32C.Compute(r));
        Put(192 * 1024, r);
        Put(256 * 1024, r);

        var m = new byte[Mib];
        Encoding.ASCII.GetBytes("metadata").CopyTo(m, 0);
        var items = new (string Guid, byte[] Data, uint Flags)[]
        {
            ("CAA16737-FA36-4D43-B3B6-33F0AA44E76B", U32Pair((uint)o.BlockSize, o.Fixed ? 1u : 0u), 4),
            ("2FA54224-CD1B-4876-B211-5DBED83BF4B8", BitConverter.GetBytes((ulong)o.VirtualSize), 6),
            ("BECA12AB-B2E6-4523-93EF-C309E000C746", Guid.NewGuid().ToByteArray(), 6),
            ("8141BF1D-A96F-4709-BA47-F233A8FAAB5F", BitConverter.GetBytes((uint)o.SectorSize), 6),
            ("CDA348C7-445D-4471-9CC9-E9885251C556", BitConverter.GetBytes(4096u), 6),
        };
        BinaryPrimitives.WriteUInt16LittleEndian(m.AsSpan(10), (ushort)items.Length);
        var dataOffset = 64 * 1024;
        for (var i = 0; i < items.Length; i++)
        {
            var e = m.AsSpan(32 + i * 32);
            new Guid(items[i].Guid).TryWriteBytes(e);
            BinaryPrimitives.WriteUInt32LittleEndian(e[16..], (uint)dataOffset);
            BinaryPrimitives.WriteUInt32LittleEndian(e[20..], (uint)items[i].Data.Length);
            BinaryPrimitives.WriteUInt32LittleEndian(e[24..], items[i].Flags);
            items[i].Data.CopyTo(m, dataOffset);
            dataOffset += 4096;
        }
        Put(2 * Mib, m);

        var bat = new byte[batBytes];
        for (var b = 0; b < dataBlocks; b++)
        {
            var index = b + b / chunkRatio;
            var entry = present[b] ? 6UL | ((ulong)(blockOffsets[b] / Mib) << 20) : 0UL;
            BinaryPrimitives.WriteUInt64LittleEndian(bat.AsSpan((int)(index * 8)), entry);
        }
        Put(3 * Mib, bat);

        for (var b = 0; b < dataBlocks; b++)
        {
            var start = b * (long)o.BlockSize;
            if (present[b] && start < content.Length)
            {
                Put(blockOffsets[b], content.Slice((int)start, (int)Math.Min(o.BlockSize, content.Length - start)));
            }
        }
    }

    private static byte[] U32Pair(uint a, uint b)
    {
        var data = new byte[8];
        BinaryPrimitives.WriteUInt32LittleEndian(data, a);
        BinaryPrimitives.WriteUInt32LittleEndian(data.AsSpan(4), b);
        return data;
    }
}
