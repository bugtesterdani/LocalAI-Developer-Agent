using System.Buffers.Binary;
using System.Formats.Tar;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;

namespace LocalAi.Sandbox;

/// <summary>
/// Schreibt dynamische VHDX-Dateien direkt als Datei – ohne sie im Host-Betriebssystem einzubinden (Rechner, auf denen
/// das Einbinden virtueller Festplatten per Richtlinie gesperrt ist; nur das Anhängen an eine VM ist erlaubt).
/// Aufbau nach [MS-VHDX]: Kennung, zwei Header, zwei Regionstabellen, leeres Log, Metadaten, BAT, Datenblöcke;
/// Nullblöcke werden nicht gespeichert.
/// </summary>
public static class VhdxWriter
{
    public const int BlockSize = 32 * 1024 * 1024;
    private const long Mib = 1024 * 1024;
    private const int SectorSize = 512;
    private static readonly Guid BatRegion = new("2DC27766-F623-4200-9D64-115E9BFD4A08");
    private static readonly Guid MetadataRegion = new("8B7CA206-4790-4B9A-B8FE-575F050F886E");
    private static readonly Guid FileParameters = new("CAA16737-FA36-4D43-B3B6-33F0AA44E76B");
    private static readonly Guid VirtualDiskSize = new("2FA54224-CD1B-4876-B211-5DBED83BF4B8");
    private static readonly Guid Page83Data = new("BECA12AB-B2E6-4523-93EF-C309E000C746");
    private static readonly Guid LogicalSectorSize = new("8141BF1D-A96F-4709-BA47-F233A8FAAB5F");
    private static readonly Guid PhysicalSectorSize = new("CDA348C7-445D-4471-9CC9-E9885251C556");

    /// <summary>
    /// Legt <paramref name="path"/> als dynamische VHDX der Größe <paramref name="virtualSize"/> an und füllt sie mit
    /// <paramref name="content"/> ab Position 0 (null = leer). Rückgabe: geschriebene Datenbytes (ohne Nullblöcke).
    /// </summary>
    public static long Write(string path, long virtualSize, Stream? content, CancellationToken cancellationToken = default)
    {
        if (virtualSize <= 0 || virtualSize % Mib != 0)
        {
            throw new ArgumentException("Größe muss ein Vielfaches von 1 MiB sein", nameof(virtualSize));
        }
        var chunkRatio = (1L << 23) * SectorSize / BlockSize;
        var dataBlocks = (virtualSize + BlockSize - 1) / BlockSize;
        var entries = dataBlocks + (dataBlocks - 1) / chunkRatio;
        var batLength = (entries * 8 + Mib - 1) / Mib * Mib;
        const long logOffset = 1 * Mib, metadataOffset = 2 * Mib, batOffset = 3 * Mib;
        var dataOffset = batOffset + batLength;
        var bat = new byte[entries * 8];
        long written = 0;

        using var file = new FileStream(path, FileMode.Create, FileAccess.ReadWrite, FileShare.None, 1 << 20);
        // Kennung
        var identifier = new byte[64 * 1024];
        Encoding.ASCII.GetBytes("vhdxfile").CopyTo(identifier, 0);
        Encoding.Unicode.GetBytes("LocalAi").CopyTo(identifier, 8);
        file.Write(identifier);

        // Datenblöcke (Nullblöcke bleiben „nicht vorhanden“ und lesen sich als Nullen).
        var next = dataOffset;
        if (content is not null)
        {
            var buffer = new byte[BlockSize];
            for (long block = 0; block < dataBlocks; block++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                var read = 0;
                while (read < BlockSize)
                {
                    var n = content.Read(buffer, read, BlockSize - read);
                    if (n <= 0)
                    {
                        break;
                    }
                    read += n;
                }
                if (read == 0)
                {
                    break;
                }
                if (read < BlockSize)
                {
                    buffer.AsSpan(read).Clear();
                }
                if (buffer.AsSpan().IndexOfAnyExcept((byte)0) < 0)
                {
                    continue;
                }
                file.Position = next;
                file.Write(buffer);
                var index = block + block / chunkRatio;
                BinaryPrimitives.WriteUInt64LittleEndian(bat.AsSpan((int)(index * 8)), 6UL | ((ulong)(next / Mib) << 20));
                next += BlockSize;
                written += read;
            }
            if (content.Read(new byte[1]) > 0)
            {
                throw new ArgumentException("Inhalt ist größer als das Laufwerk", nameof(content));
            }
        }
        file.SetLength(Math.Max(next, dataOffset));

        // Header (zwei Kopien), Regionstabelle (zwei Kopien), leeres Log, Metadaten, BAT.
        var header = new byte[4096];
        Encoding.ASCII.GetBytes("head").CopyTo(header, 0);
        BinaryPrimitives.WriteUInt64LittleEndian(header.AsSpan(8), 1);
        Guid.NewGuid().TryWriteBytes(header.AsSpan(16));
        Guid.NewGuid().TryWriteBytes(header.AsSpan(32));
        // LogGuid (48) bleibt leer: kein offenes Protokoll.
        BinaryPrimitives.WriteUInt16LittleEndian(header.AsSpan(64), 0);
        BinaryPrimitives.WriteUInt16LittleEndian(header.AsSpan(66), 1);
        BinaryPrimitives.WriteUInt32LittleEndian(header.AsSpan(68), (uint)Mib);
        BinaryPrimitives.WriteUInt64LittleEndian(header.AsSpan(72), logOffset);
        SetChecksum(header);
        WriteAt(file, 64 * 1024, header);
        WriteAt(file, 128 * 1024, header);

        var regions = new byte[64 * 1024];
        Encoding.ASCII.GetBytes("regi").CopyTo(regions, 0);
        BinaryPrimitives.WriteUInt32LittleEndian(regions.AsSpan(8), 2);
        void Region(int i, Guid id, long offset, long length)
        {
            var e = regions.AsSpan(16 + i * 32, 32);
            id.TryWriteBytes(e);
            BinaryPrimitives.WriteUInt64LittleEndian(e[16..], (ulong)offset);
            BinaryPrimitives.WriteUInt32LittleEndian(e[24..], (uint)length);
            BinaryPrimitives.WriteUInt32LittleEndian(e[28..], 1);
        }
        Region(0, BatRegion, batOffset, batLength);
        Region(1, MetadataRegion, metadataOffset, Mib);
        SetChecksum(regions);
        WriteAt(file, 192 * 1024, regions);
        WriteAt(file, 256 * 1024, regions);
        WriteAt(file, logOffset, new byte[Mib]);

        var metadata = new byte[Mib];
        Encoding.ASCII.GetBytes("metadata").CopyTo(metadata, 0);
        var items = new (Guid Id, byte[] Data, uint Flags)[]
        {
            (FileParameters, Le32Pair(BlockSize, 0), 4),
            (VirtualDiskSize, BitConverter.GetBytes((ulong)virtualSize), 6),
            (Page83Data, Guid.NewGuid().ToByteArray(), 6),
            (LogicalSectorSize, BitConverter.GetBytes((uint)SectorSize), 6),
            (PhysicalSectorSize, BitConverter.GetBytes(4096u), 6),
        };
        BinaryPrimitives.WriteUInt16LittleEndian(metadata.AsSpan(10), (ushort)items.Length);
        var itemOffset = 64 * 1024;
        for (var i = 0; i < items.Length; i++)
        {
            var e = metadata.AsSpan(32 + i * 32, 32);
            items[i].Id.TryWriteBytes(e);
            BinaryPrimitives.WriteUInt32LittleEndian(e[16..], (uint)itemOffset);
            BinaryPrimitives.WriteUInt32LittleEndian(e[20..], (uint)items[i].Data.Length);
            BinaryPrimitives.WriteUInt32LittleEndian(e[24..], items[i].Flags);
            items[i].Data.CopyTo(metadata, itemOffset);
            itemOffset += 8 * 1024;
        }
        WriteAt(file, metadataOffset, metadata);
        var batRegion = new byte[batLength];
        bat.CopyTo(batRegion, 0);
        WriteAt(file, batOffset, batRegion);
        file.Flush(flushToDisk: true);
        return written;
    }

    /// <summary>Rohabbild (z. B. debian-13-generic-amd64.raw) als VHDX der Größe <paramref name="virtualSize"/>.</summary>
    public static long FromRaw(string rawPath, string vhdxPath, long virtualSize, CancellationToken cancellationToken = default)
    {
        using var raw = new FileStream(rawPath, FileMode.Open, FileAccess.Read, FileShare.Read, 1 << 20, FileOptions.SequentialScan);
        if (raw.Length > virtualSize)
        {
            throw new InvalidDataException($"Systemfestplatte ({virtualSize >> 30} GB) ist kleiner als das Abbild ({(raw.Length + (1 << 30) - 1) >> 30} GB)");
        }
        return Write(vhdxPath, virtualSize, raw, cancellationToken);
    }

    private static byte[] Le32Pair(int a, int b)
    {
        var data = new byte[8];
        BinaryPrimitives.WriteUInt32LittleEndian(data, (uint)a);
        BinaryPrimitives.WriteUInt32LittleEndian(data.AsSpan(4), (uint)b);
        return data;
    }

    private static void SetChecksum(byte[] data)
    {
        BinaryPrimitives.WriteUInt32LittleEndian(data.AsSpan(4), 0);
        BinaryPrimitives.WriteUInt32LittleEndian(data.AsSpan(4), Crc32C.Compute(data));
    }

    private static void WriteAt(FileStream file, long offset, byte[] data)
    {
        file.Position = offset;
        file.Write(data);
    }
}

/// <summary>
/// ISO-9660-Abbild mit Joliet-Namen, nur Dateien im Wurzelordner – für Seed-, Setup- und Eingangsmedien, die als
/// DVD an die VM gehängt werden (ohne Einbinden auf dem Host). Dateien über 4 GiB vorher teilen (<see cref="Split"/>).
/// </summary>
public static class IsoWriter
{
    private const int Sector = 2048;
    public const long MaxFileBytes = 4L * 1024 * 1024 * 1024 - Sector;

    public static void Write(string isoPath, string volumeId, IReadOnlyList<(string Name, string Source)> files)
    {
        if (files.Count == 0 || files.Count > 200)
        {
            throw new ArgumentException("1 bis 200 Dateien", nameof(files));
        }
        foreach (var (name, source) in files)
        {
            if (name.Length is 0 or > 64 || name.Any(c => c is '/' or '\\' or ';' or ':' || char.IsControl(c)))
            {
                throw new ArgumentException($"Dateiname für das Abbild ungültig: {name}", nameof(files));
            }
            if (new FileInfo(source).Length > MaxFileBytes)
            {
                throw new ArgumentException($"{name} ist größer als 4 GiB – vorher teilen", nameof(files));
            }
        }
        var shortNames = ShortNames(files.Select(f => f.Name).ToList());
        var now = DateTime.UtcNow;

        // Platz: 16 Systemsektoren, PVD, SVD, Abschluss, 4 Pfadtabellen, dann die beiden Wurzelordner, dann Daten.
        byte[] Record(string? name, byte[] id, uint extent, uint size, bool directory)
        {
            var len = 33 + id.Length + (id.Length % 2 == 0 ? 1 : 0);
            var r = new byte[len];
            r[0] = (byte)len;
            Both32(r.AsSpan(2), extent);
            Both32(r.AsSpan(10), size);
            r[18] = (byte)(now.Year - 1900);
            r[19] = (byte)now.Month;
            r[20] = (byte)now.Day;
            r[21] = (byte)now.Hour;
            r[22] = (byte)now.Minute;
            r[23] = (byte)now.Second;
            r[25] = (byte)(directory ? 2 : 0);
            Both16(r.AsSpan(28), 1);
            r[32] = (byte)id.Length;
            id.CopyTo(r, 33);
            _ = name;
            return r;
        }
        var sizes = files.Select(f => (uint)new FileInfo(f.Source).Length).ToList();
        var primaryIds = shortNames.Select(n => Encoding.ASCII.GetBytes(n + ";1")).ToList();
        var jolietIds = files.Select(f => Encoding.BigEndianUnicode.GetBytes(f.Name)).ToList(); // ohne ";1" wie genisoimage
        int DirSectors(List<byte[]> ids)
        {
            var bytes = 34 * 2;
            var sectors = 1;
            var used = bytes;
            foreach (var id in ids)
            {
                var len = 33 + id.Length + (id.Length % 2 == 0 ? 1 : 0);
                if (used + len > Sector)
                {
                    sectors++;
                    used = 0;
                }
                used += len;
            }
            return sectors;
        }
        const uint pathTables = 19;
        var primaryRoot = pathTables + 4;
        var primarySectors = DirSectors(primaryIds);
        var jolietRoot = (uint)(primaryRoot + primarySectors);
        var jolietSectors = DirSectors(jolietIds);
        var nextExtent = (uint)(jolietRoot + jolietSectors);
        var extents = new List<uint>();
        foreach (var size in sizes)
        {
            extents.Add(nextExtent);
            nextExtent += (uint)Math.Max(1, (size + Sector - 1) / Sector);
        }
        var totalSectors = nextExtent;

        byte[] Directory(uint self, int sectors, List<byte[]> ids, IComparer<int> order)
        {
            var data = new byte[sectors * Sector];
            var pos = 0;
            void Add(byte[] rec)
            {
                if (pos % Sector + rec.Length > Sector)
                {
                    pos = (pos / Sector + 1) * Sector;
                }
                rec.CopyTo(data, pos);
                pos += rec.Length;
            }
            Add(Record(null, [0], self, (uint)(sectors * Sector), true));
            Add(Record(null, [1], self, (uint)(sectors * Sector), true));
            foreach (var i in Enumerable.Range(0, ids.Count).Order(order))
            {
                Add(Record(null, ids[i], extents[i], sizes[i], false));
            }
            return data;
        }
        var primaryDir = Directory(primaryRoot, primarySectors, primaryIds,
            Comparer<int>.Create((a, b) => string.CompareOrdinal(shortNames[a], shortNames[b])));
        var jolietDir = Directory(jolietRoot, jolietSectors, jolietIds,
            Comparer<int>.Create((a, b) => jolietIds[a].AsSpan().SequenceCompareTo(jolietIds[b])));

        byte[] Descriptor(byte type, uint root, int rootSectors, uint lTable, uint mTable, bool joliet)
        {
            var d = new byte[Sector];
            d[0] = type;
            Encoding.ASCII.GetBytes("CD001").CopyTo(d, 1);
            d[6] = 1;
            void Text(int offset, int length, string value)
            {
                if (joliet)
                {
                    var bytes = Encoding.BigEndianUnicode.GetBytes(value.PadRight((length + 1) / 2));
                    bytes.AsSpan(0, length).CopyTo(d.AsSpan(offset));
                }
                else
                {
                    Encoding.ASCII.GetBytes(value.PadRight(length)[..length]).CopyTo(d, offset);
                }
            }
            Text(8, 32, "");
            Text(40, 32, volumeId);
            Both32(d.AsSpan(80), totalSectors);
            if (joliet)
            {
                d[88] = 0x25;
                d[89] = 0x2F;
                d[90] = 0x45;
            }
            Both16(d.AsSpan(120), 1);
            Both16(d.AsSpan(124), 1);
            Both16(d.AsSpan(128), Sector);
            Both32(d.AsSpan(132), 10);
            BinaryPrimitives.WriteUInt32LittleEndian(d.AsSpan(140), lTable);
            BinaryPrimitives.WriteUInt32BigEndian(d.AsSpan(148), mTable);
            Record(null, [0], root, (uint)(rootSectors * Sector), true).AsSpan(0, 34).CopyTo(d.AsSpan(156));
            foreach (var offset in new[] { 190, 318, 446, 574 })
            {
                Text(offset, 128, "");
            }
            Text(702, 37, "");
            Text(739, 37, "");
            Text(776, 37, "");
            var date = Encoding.ASCII.GetBytes(now.ToString("yyyyMMddHHmmss00", System.Globalization.CultureInfo.InvariantCulture));
            date.CopyTo(d, 813);
            date.CopyTo(d, 830);
            Encoding.ASCII.GetBytes("0000000000000000").CopyTo(d, 847);
            Encoding.ASCII.GetBytes("0000000000000000").CopyTo(d, 864);
            d[881] = 1;
            return d;
        }
        byte[] PathTable(uint root, bool bigEndian)
        {
            var t = new byte[Sector];
            t[0] = 1;
            if (bigEndian)
            {
                BinaryPrimitives.WriteUInt32BigEndian(t.AsSpan(2), root);
                BinaryPrimitives.WriteUInt16BigEndian(t.AsSpan(6), 1);
            }
            else
            {
                BinaryPrimitives.WriteUInt32LittleEndian(t.AsSpan(2), root);
                BinaryPrimitives.WriteUInt16LittleEndian(t.AsSpan(6), 1);
            }
            return t;
        }

        using var iso = new FileStream(isoPath, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20);
        iso.Write(new byte[16 * Sector]);
        iso.Write(Descriptor(1, primaryRoot, primarySectors, pathTables, pathTables + 1, false));
        iso.Write(Descriptor(2, jolietRoot, jolietSectors, pathTables + 2, pathTables + 3, true));
        var terminator = new byte[Sector];
        terminator[0] = 255;
        Encoding.ASCII.GetBytes("CD001").CopyTo(terminator, 1);
        terminator[6] = 1;
        iso.Write(terminator);
        iso.Write(PathTable(primaryRoot, false));
        iso.Write(PathTable(primaryRoot, true));
        iso.Write(PathTable(jolietRoot, false));
        iso.Write(PathTable(jolietRoot, true));
        iso.Write(primaryDir);
        iso.Write(jolietDir);
        for (var i = 0; i < files.Count; i++)
        {
            iso.Position = (long)extents[i] * Sector;
            using (var source = File.OpenRead(files[i].Source))
            {
                source.CopyTo(iso, 1 << 20);
            }
        }
        iso.SetLength((long)totalSectors * Sector);
    }

    /// <summary>
    /// Teilt eine Datei über <see cref="MaxFileBytes"/> in Teile <c>name.000</c>, <c>name.001</c> …; kleinere Dateien
    /// bleiben unverändert. Rückgabe: (Name im Abbild, Quelldatei).
    /// </summary>
    public static List<(string Name, string Source)> Split(string name, string source, long partBytes = MaxFileBytes)
    {
        var length = new FileInfo(source).Length;
        if (length <= partBytes)
        {
            return [(name, source)];
        }
        var parts = new List<(string, string)>();
        using var input = File.OpenRead(source);
        var buffer = new byte[1 << 20];
        for (var part = 0; input.Position < length; part++)
        {
            var target = $"{source}.{part:D3}";
            using (var output = File.Create(target))
            {
                long left = partBytes;
                int read;
                while (left > 0 && (read = input.Read(buffer, 0, (int)Math.Min(buffer.Length, left))) > 0)
                {
                    output.Write(buffer, 0, read);
                    left -= read;
                }
            }
            parts.Add(($"{name}.{part:D3}", target));
        }
        return parts;
    }

    private static List<string> ShortNames(List<string> names)
    {
        var used = new HashSet<string>(StringComparer.Ordinal);
        var result = new List<string>();
        foreach (var name in names)
        {
            static string Clean(string s) => new(s.ToUpperInvariant().Select(c => c is >= 'A' and <= 'Z' or >= '0' and <= '9' ? c : '_').ToArray());
            var dot = name.LastIndexOf('.');
            var stem = Clean(dot > 0 ? name[..dot] : name);
            var ext = Clean(dot > 0 ? name[(dot + 1)..] : "");
            stem = stem.Length > 8 ? stem[..8] : stem;
            ext = ext.Length > 3 ? ext[..3] : ext;
            var candidate = ext.Length > 0 ? $"{stem}.{ext}" : stem + ".";
            for (var n = 1; !used.Add(candidate); n++)
            {
                var suffix = n.ToString(System.Globalization.CultureInfo.InvariantCulture);
                var s = stem[..Math.Min(stem.Length, 8 - suffix.Length)] + suffix;
                candidate = ext.Length > 0 ? $"{s}.{ext}" : s + ".";
            }
            result.Add(candidate);
        }
        return result;
    }

    private static void Both16(Span<byte> span, ushort value)
    {
        BinaryPrimitives.WriteUInt16LittleEndian(span, value);
        BinaryPrimitives.WriteUInt16BigEndian(span[2..], value);
    }

    private static void Both32(Span<byte> span, uint value)
    {
        BinaryPrimitives.WriteUInt32LittleEndian(span, value);
        BinaryPrimitives.WriteUInt32BigEndian(span[4..], value);
    }
}

/// <summary>
/// Eingangs-Laufwerk (Betriebsart ohne Copy-VMFile): der Host schreibt die Dateien eines Jobs als tar in eine neue
/// VHDX und hängt sie an die VM; der Worker im Gast erkennt sie am Kopf, prüft Prüfsumme und Sequenz und legt die
/// Dateien in seinen Eingang. Aufbau: 4096-Byte-Kopf (<c>LAIINBX1</c>, Version, Sequenz, tar-Länge, SHA-256), dann tar.
/// </summary>
public static class InboundDisk
{
    public const int HeaderSize = 4096;
    private static readonly byte[] Magic = "LAIINBX1"u8.ToArray();
    private const long Mib = 1024 * 1024;

    /// <summary>Schreibt <paramref name="files"/> (flach, nur Dateinamen) als Eingangs-Laufwerk nach <paramref name="vhdxPath"/>.</summary>
    public static void Write(string vhdxPath, long sequence, IReadOnlyList<string> files, long avoidSize)
    {
        var tarPath = vhdxPath + ".tar";
        try
        {
            using (var tar = File.Create(tarPath))
            using (var writer = new TarWriter(tar, TarEntryFormat.Ustar, leaveOpen: true))
            {
                foreach (var file in files)
                {
                    var name = Path.GetFileName(file);
                    if (!QemuVmHost.SafeFileName.IsMatch(name))
                    {
                        throw new ArgumentException($"Dateiname für den Gast nicht erlaubt: {name}", nameof(files));
                    }
                    writer.WriteEntry(file, name);
                }
            }
            byte[] hash;
            long length;
            using (var tar = File.OpenRead(tarPath))
            {
                length = tar.Length;
                hash = SHA256.HashData(tar);
            }
            var header = new byte[HeaderSize];
            Magic.CopyTo(header, 0);
            BinaryPrimitives.WriteUInt32LittleEndian(header.AsSpan(8), 1);
            BinaryPrimitives.WriteUInt64LittleEndian(header.AsSpan(16), (ulong)sequence);
            BinaryPrimitives.WriteUInt64LittleEndian(header.AsSpan(24), (ulong)length);
            hash.CopyTo(header, 32);
            var size = Math.Max(32 * Mib, (HeaderSize + length + Mib - 1) / Mib * Mib);
            if (size == avoidSize)
            {
                size += Mib; // nie so groß wie das Transfer-Laufwerk (der Gast erkennt dieses an seiner Größe)
            }
            using var content = new ConcatStream(new MemoryStream(header), File.OpenRead(tarPath));
            VhdxWriter.Write(vhdxPath, size, content);
        }
        finally
        {
            File.Delete(tarPath);
        }
    }

    /// <summary>Liest ein Eingangs-Laufwerk (Tests und Gegenprobe): Sequenz und tar-Inhalt.</summary>
    public static (long Sequence, byte[] Tar) Read(string vhdxPath)
    {
        using var disk = VhdxReader.Open(vhdxPath, 1L << 40);
        var header = new byte[HeaderSize];
        disk.ReadAt(0, header);
        if (!header.AsSpan(0, 8).SequenceEqual(Magic))
        {
            throw new VhdxFormatException("Kein Eingangs-Laufwerk");
        }
        var length = (long)BinaryPrimitives.ReadUInt64LittleEndian(header.AsSpan(24));
        var tar = new byte[length];
        disk.ReadAt(HeaderSize, tar);
        if (!SHA256.HashData(tar).AsSpan().SequenceEqual(header.AsSpan(32, 32)))
        {
            throw new VhdxFormatException("Prüfsumme stimmt nicht");
        }
        return ((long)BinaryPrimitives.ReadUInt64LittleEndian(header.AsSpan(16)), tar);
    }

    private sealed class ConcatStream(Stream first, Stream second) : Stream
    {
        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override void Flush() { }
        public override int Read(byte[] buffer, int offset, int count)
        {
            var n = first.Read(buffer, offset, count);
            return n > 0 ? n : second.Read(buffer, offset, count);
        }
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                first.Dispose();
                second.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}

/// <summary>
/// Aufbaumedien ohne Einbinden auf dem Host (<see cref="HyperVSettings.HostMount"/> = false):
/// Linux – Systemfestplatte als VHDX aus dem Debian-Abbild und Seed als ISO (CIDATA);
/// Windows – Antwortdatei, Gastdateien und Einrichtungsskript als ISO für Windows Setup.
/// </summary>
public static class SetupMedia
{
    public const string LinuxOsDisk = "os.vhdx";
    public const string SeedIso = "seed.iso";
    public const string UnattendIso = "unattend.iso";

    /// <summary>Linux: <c>os.vhdx</c> und <c>seed.iso</c> (user-data, meta-data, localai.tar) im Gastordner.</summary>
    public static void BuildLinux(string guestDirectory, HyperVSettings settings, CancellationToken cancellationToken = default)
    {
        var seed = Path.Combine(guestDirectory, "seed");
        var tarPath = Path.Combine(guestDirectory, "localai.tar");
        using (var tar = File.Create(tarPath))
        {
            TarFile.CreateFromDirectory(Path.Combine(seed, "localai"), tar, includeBaseDirectory: true);
        }
        var files = new List<(string, string)> { ("user-data", Path.Combine(seed, "user-data")), ("meta-data", Path.Combine(seed, "meta-data")) };
        files.AddRange(IsoWriter.Split("localai.tar", tarPath));
        IsoWriter.Write(Path.Combine(guestDirectory, SeedIso), "CIDATA", files);
        foreach (var (_, source) in files.Where(f => f.Item2.StartsWith(tarPath, StringComparison.Ordinal)))
        {
            File.Delete(source);
        }
        File.Delete(tarPath);
        // Seed-Ordner wird nicht mehr gebraucht (enthält den Sitzungsschlüssel nur noch im Abbild).
        FileTools.DeleteTree(seed);
        VhdxWriter.FromRaw(settings.ImagePath, Path.Combine(guestDirectory, LinuxOsDisk), settings.OsDiskGB * 1024L * 1024 * 1024, cancellationToken);
    }

    /// <summary>Windows: <c>unattend.iso</c> mit autounattend.xml, Einrichtungsskript, SetupComplete.cmd und LocalAi.zip.</summary>
    public static string BuildWindows(string guestDirectory, HyperVSettings settings)
    {
        var media = Path.Combine(guestDirectory, "media");
        Directory.CreateDirectory(media);
        var password = "Lai-" + Convert.ToBase64String(RandomNumberGenerator.GetBytes(24)).Replace('+', 'x').Replace('/', 'y').TrimEnd('=') + "9!";
        var passwordFile = Path.Combine(settings.VmDirectory, "gast-kennwort.txt");
        if (settings.KeepVmOnExit)
        {
            Directory.CreateDirectory(settings.VmDirectory);
            File.WriteAllText(passwordFile, password, Encoding.ASCII);
        }
        File.WriteAllText(Path.Combine(media, "autounattend.xml"), WindowsUnattend.Autounattend(settings, password), new UTF8Encoding(false));
        File.WriteAllText(Path.Combine(media, "localai-setup.cmd"), WindowsUnattend.SetupCmd, Encoding.ASCII);
        File.WriteAllText(Path.Combine(media, "localai-setup.ps1"), WindowsUnattend.SetupPs1, new UTF8Encoding(true));
        File.Copy(Path.Combine(guestDirectory, "SetupComplete.cmd"), Path.Combine(media, "SetupComplete.cmd"));
        var zip = Path.Combine(guestDirectory, "LocalAi.zip");
        ZipFile.CreateFromDirectory(Path.Combine(guestDirectory, "LocalAi"), zip, CompressionLevel.Fastest, includeBaseDirectory: true);
        var files = Directory.EnumerateFiles(media).Order(StringComparer.Ordinal).Select(f => (Path.GetFileName(f), f)).ToList();
        files.AddRange(IsoWriter.Split("LocalAi.zip", zip));
        IsoWriter.Write(Path.Combine(guestDirectory, UnattendIso), "LOCALAI", files);
        foreach (var (_, source) in files.Where(f => f.Item2.StartsWith(zip, StringComparison.Ordinal)))
        {
            File.Delete(source);
        }
        File.Delete(zip);
        FileTools.DeleteTree(media);
        // Gastdateien (mit Sitzungsschlüssel) liegen jetzt nur noch im Abbild.
        FileTools.DeleteTree(Path.Combine(guestDirectory, "LocalAi"));
        return password;
    }
}

/// <summary>autounattend.xml für Windows Setup von der ISO (ohne DISM und ohne Einbinden auf dem Host).</summary>
public static class WindowsUnattend
{
    private const string Common = "processorArchitecture=\"amd64\" publicKeyToken=\"31bf3856ad364e35\" language=\"neutral\" versionScope=\"nonSxS\"";

    /// <summary>Im specialize-Durchlauf von der Setup-DVD aufgerufen: Gastdateien nach C:\LocalAi, SetupComplete.cmd.</summary>
    public const string SetupCmd = "@echo off\r\n"
        + "rem LocalAi: Gastdateien aus dem Setup-Abbild uebernehmen (specialize).\r\n"
        + "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"%~d0\\localai-setup.ps1\" -Media %~d0\r\n";

    public const string SetupPs1 = """
        param([Parameter(Mandatory)][string]$Media)
        $ErrorActionPreference = 'Stop'
        $log = Join-Path $env:SystemRoot 'Temp\localai-setup.log'
        Start-Transcript -LiteralPath $log -Force | Out-Null
        try {
            $zip = Join-Path $env:SystemRoot 'Temp\LocalAi.zip'
            $parts = @(Get-ChildItem -LiteralPath "$Media\" -Filter 'LocalAi.zip*' | Sort-Object Name)
            $out = [System.IO.File]::Create($zip)
            try { foreach ($p in $parts) { $in = [System.IO.File]::OpenRead($p.FullName); try { $in.CopyTo($out) } finally { $in.Dispose() } } }
            finally { $out.Dispose() }
            & (Join-Path $env:SystemRoot 'System32\tar.exe') -xf $zip -C "$env:SystemDrive\"
            if ($LASTEXITCODE -ne 0) { throw "tar.exe endete mit $LASTEXITCODE" }
            Remove-Item -LiteralPath $zip -Force
            $scripts = Join-Path $env:SystemRoot 'Setup\Scripts'
            New-Item -ItemType Directory -Force -Path $scripts | Out-Null
            Copy-Item -LiteralPath "$Media\SetupComplete.cmd" -Destination $scripts -Force
        } finally { Stop-Transcript | Out-Null }
        """;

    public static string AdminGroup(string language) => language.ToLowerInvariant() switch
    {
        var l when l.StartsWith("de", StringComparison.Ordinal) => "Administratoren",
        var l when l.StartsWith("fr", StringComparison.Ordinal) => "Administrateurs",
        var l when l.StartsWith("es", StringComparison.Ordinal) || l.StartsWith("pt", StringComparison.Ordinal) => "Administradores",
        _ => "Administrators",
    };

    public static string Autounattend(HyperVSettings settings, string password)
    {
        static string E(string s) => System.Security.SecurityElement.Escape(s);
        var language = settings.ImageLanguage;
        var image = settings.ImageIndex is { } index
            ? $"<Key>/IMAGE/INDEX</Key><Value>{index}</Value>"
            : $"<Key>/IMAGE/NAME</Key><Value>{E(settings.ImageName ?? "")}</Value>";
        var key = settings.ProductKey is { } k
            ? $"""
                <ProductKey>
                          <Key>{E(k)}</Key>
                          <WillShowUI>OnError</WillShowUI>
                        </ProductKey>
              """
            : "";
        string Bypass(int order, string name) =>
            $"""
                    <RunSynchronousCommand wcm:action="add">
                      <Order>{order}</Order>
                      <Path>reg.exe add HKLM\SYSTEM\Setup\LabConfig /v {name} /t REG_DWORD /d 1 /f</Path>
                    </RunSynchronousCommand>
            """;
        return $"""
            <?xml version="1.0" encoding="utf-8"?>
            <!-- LocalAi: Windows Setup ohne DISM (hostMount: false). Ohne Netzwerkkarte, Gastdateien von der Setup-DVD. -->
            <unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
              <settings pass="windowsPE">
                <component name="Microsoft-Windows-International-Core-WinPE" {Common}>
                  <SetupUILanguage>
                    <UILanguage>{E(language)}</UILanguage>
                  </SetupUILanguage>
                  <InputLocale>de-DE</InputLocale>
                  <SystemLocale>{E(language)}</SystemLocale>
                  <UILanguage>{E(language)}</UILanguage>
                  <UserLocale>de-DE</UserLocale>
                </component>
                <component name="Microsoft-Windows-Setup" {Common}>
                  <RunSynchronous>
            {Bypass(1, "BypassTPMCheck")}
            {Bypass(2, "BypassSecureBootCheck")}
            {Bypass(3, "BypassRAMCheck")}
                  </RunSynchronous>
                  <DiskConfiguration>
                    <Disk wcm:action="add">
                      <DiskID>0</DiskID>
                      <WillWipeDisk>true</WillWipeDisk>
                      <CreatePartitions>
                        <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>260</Size></CreatePartition>
                        <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
                        <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
                      </CreatePartitions>
                      <ModifyPartitions>
                        <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
                        <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
                      </ModifyPartitions>
                    </Disk>
                  </DiskConfiguration>
                  <ImageInstall>
                    <OSImage>
                      <InstallFrom>
                        <MetaData wcm:action="add">{image}</MetaData>
                      </InstallFrom>
                      <InstallTo>
                        <DiskID>0</DiskID>
                        <PartitionID>3</PartitionID>
                      </InstallTo>
                    </OSImage>
                  </ImageInstall>
                  <UserData>
                    <AcceptEula>true</AcceptEula>
            {key}
                  </UserData>
                </component>
              </settings>
              <settings pass="specialize">
                <component name="Microsoft-Windows-Shell-Setup" {Common}>
                  <ComputerName>LOCALAI-SBX</ComputerName>
                  <TimeZone>W. Europe Standard Time</TimeZone>
                </component>
                <component name="Microsoft-Windows-Deployment" {Common}>
                  <RunSynchronous>
                    <RunSynchronousCommand wcm:action="add">
                      <Order>1</Order>
                      <Path>reg.exe add HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path>
                    </RunSynchronousCommand>
                    <RunSynchronousCommand wcm:action="add">
                      <Order>2</Order>
                      <Path>cmd.exe /c for %i in (D E F G H I J K L M N O P Q R S T U V W X Y Z) do @if exist %i:\localai-setup.cmd call %i:\localai-setup.cmd</Path>
                    </RunSynchronousCommand>
                  </RunSynchronous>
                </component>
              </settings>
              <settings pass="oobeSystem">
                <component name="Microsoft-Windows-International-Core" {Common}>
                  <InputLocale>de-DE</InputLocale>
                  <SystemLocale>de-DE</SystemLocale>
                  <UILanguage>{E(language)}</UILanguage>
                  <UserLocale>de-DE</UserLocale>
                </component>
                <component name="Microsoft-Windows-Shell-Setup" {Common}>
                  <OOBE>
                    <HideEULAPage>true</HideEULAPage>
                    <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
                    <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
                    <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
                    <HideLocalAccountScreen>true</HideLocalAccountScreen>
                    <ProtectYourPC>3</ProtectYourPC>
                    <SkipMachineOOBE>true</SkipMachineOOBE>
                    <SkipUserOOBE>true</SkipUserOOBE>
                  </OOBE>
                  <UserAccounts>
                    <LocalAccounts>
                      <LocalAccount wcm:action="add">
                        <Name>localai</Name>
                        <DisplayName>LocalAi</DisplayName>
                        <Group>{E(AdminGroup(language))}</Group>
                        <Password>
                          <Value>{E(password)}</Value>
                          <PlainText>true</PlainText>
                        </Password>
                      </LocalAccount>
                    </LocalAccounts>
                  </UserAccounts>
                  <TimeZone>W. Europe Standard Time</TimeZone>
                </component>
              </settings>
            </unattend>
            """;
    }
}
