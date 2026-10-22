using System.Diagnostics;
using System.Formats.Tar;
using System.Xml.Linq;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>
/// Rechner, auf denen virtuelle Festplatten nicht am Host eingebunden werden dürfen (hostMount: false, inbound: Disk):
/// VHDX und ISO werden direkt als Datei geschrieben; Windows Setup statt DISM; Eingangs-Laufwerk statt Copy-VMFile.
/// Gegenproben mit qemu-img und isoinfo, wenn vorhanden.
/// </summary>
public sealed class OhneEinbindenTests : IDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-nomount-").FullName;

    public void Dispose()
    {
        try
        {
            FileTools.DeleteTree(_dir);
        }
        catch (IOException)
        {
        }
    }

    private static string? Tool(string name) => QemuVmHost.FindProgram(name);

    private static string Run(string program, params string[] args)
    {
        var start = new ProcessStartInfo(program) { RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var a in args)
        {
            start.ArgumentList.Add(a);
        }
        using var p = Process.Start(start)!;
        var output = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd();
        p.WaitForExit();
        Assert.True(p.ExitCode == 0, $"{program} {string.Join(' ', args)}: {output}");
        return output;
    }

    [Fact, Trait("Pruefung", "Ohne-Einbinden")]
    public void Vhdx_aus_Rohabbild_liest_sich_identisch()
    {
        var raw = Path.Combine(_dir, "disk.raw");
        var data = new byte[100L * 1024 * 1024];
        Random.Shared.NextBytes(data.AsSpan(0, 3 * 1024 * 1024));
        Random.Shared.NextBytes(data.AsSpan(70 * 1024 * 1024, 1024 * 1024 + 17)); // zweiter belegter Block, dazwischen Nullblöcke
        File.WriteAllBytes(raw, data);
        var vhdx = Path.Combine(_dir, "os.vhdx");
        var written = VhdxWriter.FromRaw(raw, vhdx, 8L * 1024 * 1024 * 1024, TestContext.Current.CancellationToken);
        Assert.Equal(2L * VhdxWriter.BlockSize, written);
        Assert.True(new FileInfo(vhdx).Length < 80L * 1024 * 1024, "Nullblöcke belegen keinen Platz");
        using (var reader = VhdxReader.Open(vhdx, 16L << 30))
        {
            Assert.Equal(8L << 30, reader.VirtualSize);
            var back = new byte[data.Length];
            reader.ReadAt(0, back);
            Assert.True(back.AsSpan().SequenceEqual(data));
            var tail = new byte[4096];
            reader.ReadAt((8L << 30) - 4096, tail);
            Assert.DoesNotContain(tail, b => b != 0);
        }
        Assert.Throws<InvalidDataException>(() => VhdxWriter.FromRaw(raw, vhdx, 64L * 1024 * 1024, TestContext.Current.CancellationToken));
        if (Tool("qemu-img") is { } qemuImg)
        {
            // Unabhängige Gegenprobe: qemu-img prüft die Struktur und liest den Inhalt zurück.
            Assert.Contains("No errors", Run(qemuImg, "check", "-f", "vhdx", vhdx));
            var back = Path.Combine(_dir, "back.raw");
            Run(qemuImg, "convert", "-f", "vhdx", "-O", "raw", vhdx, back);
            using var b = File.OpenRead(back);
            var head = new byte[data.Length];
            b.ReadExactly(head);
            Assert.True(head.AsSpan().SequenceEqual(data));
        }
    }

    [Fact, Trait("Pruefung", "Ohne-Einbinden")]
    public void Iso_mit_Joliet_Namen_und_Teilung_grosser_Dateien()
    {
        var files = new List<(string, string)>();
        foreach (var (name, content) in new[] { ("user-data", "#cloud-config\n"), ("meta-data", "instance-id: x\n"), ("autounattend.xml", "<unattend/>"),
                     ("localai-setup.ps1", "param()"), ("localai.tar.000", new string('a', 5000)) })
        {
            var path = Path.Combine(_dir, name);
            File.WriteAllText(path, content);
            files.Add((name, path));
        }
        var iso = Path.Combine(_dir, "seed.iso");
        IsoWriter.Write(iso, "CIDATA", files);
        Assert.Equal(0, new FileInfo(iso).Length % 2048);
        if (Tool("isoinfo") is { } isoinfo)
        {
            var volume = Run(isoinfo, "-d", "-i", iso);
            Assert.Contains("Volume id: CIDATA", volume);
            Assert.Contains("Joliet", volume);
            var joliet = Run(isoinfo, "-J", "-f", "-i", iso);
            foreach (var (name, _) in files)
            {
                Assert.Contains("/" + name, joliet);
            }
            Assert.Equal(new string('a', 5000), Run(isoinfo, "-J", "-x", "/localai.tar.000", "-i", iso));
            Assert.Equal("<unattend/>", Run(isoinfo, "-J", "-x", "/autounattend.xml", "-i", iso));
            Assert.Contains("USER_DAT", Run(isoinfo, "-f", "-i", iso));
        }
        // Teilen: Teile ergeben zusammen das Original.
        var big = Path.Combine(_dir, "big.bin");
        var bytes = new byte[10_000];
        Random.Shared.NextBytes(bytes);
        File.WriteAllBytes(big, bytes);
        var parts = IsoWriter.Split("big.bin", big, 4096);
        Assert.Equal(["big.bin.000", "big.bin.001", "big.bin.002"], parts.Select(p => p.Name).ToArray());
        Assert.Equal(bytes, parts.SelectMany(p => File.ReadAllBytes(p.Source)).ToArray());
        Assert.Single(IsoWriter.Split("klein", big));
        Assert.Throws<ArgumentException>(() => IsoWriter.Write(iso, "X", [("a/b", big)]));
    }

    [Fact, Trait("Pruefung", "Ohne-Einbinden")]
    public void Eingangs_Laufwerk_traegt_Job_Dateien_mit_Pruefsumme()
    {
        var job = Path.Combine(_dir, "000000000007-abc.job.json");
        var ready = Path.Combine(_dir, "000000000007-abc.ready");
        File.WriteAllText(job, "{\"job\":1}");
        File.WriteAllText(ready, "x");
        var disk = Path.Combine(_dir, "inbound-000007.vhdx");
        InboundDisk.Write(disk, 7, [job, ready], avoidSize: 32L * 1024 * 1024);
        var (sequence, tar) = InboundDisk.Read(disk);
        Assert.Equal(7, sequence);
        using var reader = new TarReader(new MemoryStream(tar));
        var names = new List<string>();
        while (reader.GetNextEntry() is { } entry)
        {
            names.Add(entry.Name);
        }
        Assert.Equal(["000000000007-abc.job.json", "000000000007-abc.ready"], names);
        using (var vhdx = VhdxReader.Open(disk, 1L << 40))
        {
            Assert.NotEqual(32L * 1024 * 1024, vhdx.VirtualSize); // nie so groß wie das Transfer-Laufwerk
        }
        Assert.Throws<ArgumentException>(() => InboundDisk.Write(disk, 8, [Path.Combine(_dir, "..x")], 0));
    }

    private HyperVSettings Windows(string? key = null) => new()
    {
        ImagePath = Path.Combine(_dir, "win11.iso"),
        ImageName = "Windows 11 Pro",
        VmDirectory = Path.Combine(_dir, "vm"),
        HostMount = false,
        ProductKey = key,
    };

    [Fact, Trait("Pruefung", "Ohne-Einbinden")]
    public void Antwortdatei_fuer_Windows_Setup()
    {
        var xml = WindowsUnattend.Autounattend(Windows("VK7JG-NPHTM-C97JM-9MPGT-3V66T"), "Lai-<&>9!");
        var doc = XDocument.Parse(xml);
        XNamespace u = "urn:schemas-microsoft-com:unattend";
        var passes = doc.Root!.Elements(u + "settings").Select(s => s.Attribute("pass")!.Value).ToList();
        Assert.Equal(["windowsPE", "specialize", "oobeSystem"], passes);
        Assert.Equal("/IMAGE/NAME", doc.Descendants(u + "MetaData").Single().Element(u + "Key")!.Value);
        Assert.Equal("Windows 11 Pro", doc.Descendants(u + "MetaData").Single().Element(u + "Value")!.Value);
        Assert.Equal("Lai-<&>9!", doc.Descendants(u + "Password").Single().Element(u + "Value")!.Value);
        Assert.Equal("Administratoren", doc.Descendants(u + "Group").Single().Value);
        Assert.Contains(doc.Descendants(u + "Path"), p => p.Value.Contains("BypassTPMCheck", StringComparison.Ordinal));
        Assert.Contains(doc.Descendants(u + "Path"), p => p.Value.Contains("localai-setup.cmd", StringComparison.Ordinal));
        Assert.Equal("VK7JG-NPHTM-C97JM-9MPGT-3V66T", doc.Descendants(u + "Key").Last().Value);
        var byIndex = XDocument.Parse(WindowsUnattend.Autounattend(Windows() with { ImageName = null, ImageIndex = 6, ImageLanguage = "en-US" }, "p"));
        Assert.Equal("6", byIndex.Descendants(u + "MetaData").Single().Element(u + "Value")!.Value);
        Assert.Equal("Administrators", byIndex.Descendants(u + "Group").Single().Value);
        Assert.Empty(byIndex.Descendants(u + "ProductKey"));
    }

    [Fact, Trait("Pruefung", "Ohne-Einbinden")]
    public void Windows_Setup_Abbild_enthaelt_Gastdateien_und_keine_Reste()
    {
        var guest = Directory.CreateDirectory(Path.Combine(_dir, "guest")).FullName;
        Directory.CreateDirectory(Path.Combine(guest, "LocalAi", "python"));
        File.WriteAllText(Path.Combine(guest, "LocalAi", "session.key"), "geheim");
        File.WriteAllText(Path.Combine(guest, "LocalAi", "python", "python.exe"), "exe");
        File.WriteAllText(Path.Combine(guest, "SetupComplete.cmd"), "@echo off");
        SetupMedia.BuildWindows(guest, Windows() with { KeepVmOnExit = true });
        Assert.True(File.Exists(Path.Combine(guest, SetupMedia.UnattendIso)));
        Assert.False(Directory.Exists(Path.Combine(guest, "LocalAi")), "Gastdateien nur noch im Abbild");
        Assert.True(File.Exists(Path.Combine(_dir, "vm", "gast-kennwort.txt")));
        if (Tool("isoinfo") is { } isoinfo)
        {
            var listing = Run(isoinfo, "-J", "-f", "-i", Path.Combine(guest, SetupMedia.UnattendIso));
            foreach (var name in new[] { "/autounattend.xml", "/localai-setup.cmd", "/localai-setup.ps1", "/SetupComplete.cmd", "/LocalAi.zip" })
            {
                Assert.Contains(name, listing);
            }
        }
    }

    [Fact, Trait("Pruefung", "Ohne-Einbinden")]
    public void Kennwortdatei_des_Brokers_gilt_beim_ersten_Aufbau_nicht_als_fremd()
    {
        // BuildWindows legt gast-kennwort.txt (keepVmOnExit) vor der Besitzprüfung des Aufbauskripts in den VM-Ordner.
        foreach (var script in new[] { "New-LocalAiSandbox.ps1", "New-LocalAiLinuxSandbox.ps1" })
        {
            var text = File.ReadAllText(Path.Combine(ContractTests.RepoRoot(), "scripts", "sandbox", script));
            var filter = text.Split('\n').Single(l => l.Contains("$foreign = "));
            Assert.Contains("'gast-kennwort.txt'", filter);
            Assert.Contains("'sandbox.lock'", filter); // Sperre der Broker-Instanz, liegt vor dem ersten Aufbau im Ordner
        }
    }

    [Fact, Trait("Pruefung", "Ohne-Einbinden")]
    public void Linux_Seed_als_ISO_und_Systemfestplatte_als_VHDX()
    {
        var guest = Directory.CreateDirectory(Path.Combine(_dir, "lguest")).FullName;
        var seed = Directory.CreateDirectory(Path.Combine(guest, "seed", "localai", "prereq", "wheels")).FullName;
        File.WriteAllText(Path.Combine(guest, "seed", "user-data"), "#cloud-config\n");
        File.WriteAllText(Path.Combine(guest, "seed", "meta-data"), "instance-id: x\n");
        File.WriteAllText(Path.Combine(seed, "pydantic_core-2.41.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"), "w");
        File.WriteAllText(Path.Combine(guest, "seed", "localai", "session.key"), "geheim");
        var image = Path.Combine(_dir, "debian-13-generic-amd64.raw");
        File.WriteAllBytes(image, [0xEB, 0x63, 0x90, .. new byte[4093]]);
        var settings = new HyperVSettings
        {
            Guest = GuestOs.Linux, ImagePath = image, VmDirectory = Path.Combine(_dir, "vm"), HostMount = false, OsDiskGB = 8,
        };
        settings.Validate();
        SetupMedia.BuildLinux(guest, settings, TestContext.Current.CancellationToken);
        Assert.False(Directory.Exists(Path.Combine(guest, "seed")));
        using (var disk = VhdxReader.Open(Path.Combine(guest, SetupMedia.LinuxOsDisk), 16L << 30))
        {
            var head = new byte[3];
            disk.ReadAt(0, head);
            Assert.Equal([0xEB, 0x63, 0x90], head);
        }
        if (Tool("isoinfo") is { } isoinfo)
        {
            var iso = Path.Combine(guest, SetupMedia.SeedIso);
            Assert.Contains("Volume id: CIDATA", Run(isoinfo, "-d", "-i", iso));
            var tar = Path.Combine(_dir, "localai.tar");
            var start = new ProcessStartInfo(isoinfo) { RedirectStandardOutput = true };
            foreach (var a in new[] { "-J", "-x", "/localai.tar", "-i", iso })
            {
                start.ArgumentList.Add(a);
            }
            using (var p = Process.Start(start)!)
            using (var output = File.Create(tar))
            {
                p.StandardOutput.BaseStream.CopyTo(output);
                p.WaitForExit();
            }
            using var reader = new TarReader(File.OpenRead(tar));
            var names = new List<string>();
            while (reader.GetNextEntry() is { } entry)
            {
                names.Add(entry.Name);
            }
            Assert.Contains("localai/session.key", names);
            Assert.Contains("localai/prereq/wheels/pydantic_core-2.41.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", names);
        }
    }

    [Fact, Trait("Pruefung", "Ohne-Einbinden")]
    public void Konfiguration_liest_hostMount_inbound_und_Sprache()
    {
        var cfg = Directory.CreateDirectory(Path.Combine(_dir, "repo", "config")).FullName;
        var file = Path.Combine(cfg, "localai.json");
        File.WriteAllText(file, """
            { "sandbox": { "mode": "hyperv",
                           "hyperv": { "imagePath": "win.iso", "imageName": "Windows 11 Pro", "hostMount": false, "inbound": "Disk",
                                       "imageLanguage": "en-US" },
                           "hypervLinux": { "imagePath": "linux/debian-13-generic-amd64.raw", "hostMount": false } } }
            """);
        var options = LocalAi.Broker.Host.HostOptions.Parse(["--config", file, "--data", Path.Combine(_dir, "data", "broker")]);
        Assert.False(options.HyperV!.HostMount);
        Assert.True(options.HyperV.InboundDisk);
        Assert.Equal("en-US", options.HyperV.ImageLanguage);
        options.HyperV.Validate();
        Assert.False(options.HyperVLinux!.HostMount);
        Assert.False(options.HyperVLinux.InboundDisk);
        var template = LocalAi.Broker.Host.HostOptions.Parse(["--config", Path.Combine(ContractTests.RepoRoot(), "config", "localai.json"),
            "--data", Path.Combine(_dir, "data2")]);
        Assert.True(template.HyperV!.HostMount);
        Assert.Equal("CopyVMFile", template.HyperV.Inbound);
    }

    [Fact, Trait("Pruefung", "Ohne-Einbinden")]
    public void Einstellungen_werden_geprueft()
    {
        Windows().Validate();
        Assert.Throws<InvalidDataException>(() => (Windows() with { ImagePath = "install.wim" }).Validate());
        Assert.Throws<InvalidDataException>(() => (Windows() with { Inbound = "Netz" }).Validate());
        Assert.Throws<InvalidDataException>(() => (Windows() with { ImageLanguage = "de DE" }).Validate());
        (Windows() with { HostMount = true, ImagePath = "install.wim", Inbound = "Disk" }).Validate();
        Assert.Throws<InvalidDataException>(() => new HyperVSettings
        {
            Guest = GuestOs.Linux, Hypervisor = Hypervisor.Qemu, ImagePath = "d.raw", VmDirectory = _dir, Inbound = "Disk",
        }.Validate());
    }
}
