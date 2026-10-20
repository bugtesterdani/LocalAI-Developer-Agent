using System.Text.Json;
using System.Text.Json.Nodes;
using LocalAi.Broker.Host;
using LocalAi.Contracts;
using LocalAi.Sandbox;

namespace LocalAi.Tests;

/// <summary>
/// M10: Linux-Host mit QEMU statt Hyper-V – Konfiguration, feste QEMU-Befehlszeile ohne Netzwerkgerät, Verbindungskanal,
/// Transfer-Laufwerk im Rohformat, Gastkonfiguration. Der echte Ablauf mit QEMU: tests/integration/test_m10_qemu.py.
/// </summary>
public sealed class M10Tests : IDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("localai-m10-").FullName;

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

    private HyperVSettings Settings(Action<HyperVSettings>? check = null) => new()
    {
        Guest = GuestOs.Linux,
        Hypervisor = Hypervisor.Qemu,
        ImagePath = Path.Combine(_dir, "debian-13-generic-amd64.raw"),
        VmDirectory = Path.Combine(_dir, "vm, mit Komma"),
        VmName = "LocalAi-Linux",
        MemoryMB = 2048,
        Processors = 2,
    };

    [Fact, Trait("Pruefung", "M10-Konfiguration")]
    public void Konfiguration_liest_qemu_Abschnitt()
    {
        var repo = Directory.CreateDirectory(Path.Combine(_dir, "repo")).FullName;
        var cfg = Directory.CreateDirectory(Path.Combine(repo, "config")).FullName;
        var file = Path.Combine(cfg, "localai.json");
        File.WriteAllText(file, """
            { "sandbox": { "mode": "qemu",
                           "qemu": { "imagePath": "linux/debian-13-generic-amd64.raw", "accel": "Kvm", "idleMinutes": 15,
                                     "nestedVirtualization": true, "binary": "/opt/qemu/bin/qemu-system-x86_64" } } }
            """);
        var options = HostOptions.Parse(["--config", file, "--data", Path.Combine(_dir, "data", "broker")]);
        Assert.Equal(SandboxMode.Qemu, options.Mode);
        Assert.True(options.UsesVm);
        Assert.Equal("qemu", HostOptions.ModeName(options.Mode));
        var q = options.Qemu!;
        Assert.True(q.IsQemu && q.IsLinux);
        Assert.Equal("sandbox.qemu", q.Section);
        Assert.Equal("Kvm", q.QemuAccel);
        Assert.Equal("/opt/qemu/bin/qemu-system-x86_64", q.QemuBinary);
        Assert.True(q.NestedVirtualization);
        Assert.Equal(15, q.IdleMinutes);
        Assert.Equal(Path.Combine(_dir, "data", "vm-qemu"), q.VmDirectory);
        Assert.Equal(Path.Combine(repo, "linux", "linux-gast"), q.LinuxPrerequisites);
        Assert.Null(q.QemuTestBoot);
        q.Validate();
    }

    [Fact, Trait("Pruefung", "M10-Konfiguration")]
    public void Ungueltige_qemu_Einstellungen_werden_abgelehnt()
    {
        Assert.Throws<InvalidDataException>(() => (Settings() with { StopAction = "Save" }).Validate());
        Assert.Throws<InvalidDataException>(() => (Settings() with { QemuAccel = "Hvf" }).Validate());
        Assert.Throws<InvalidDataException>(() => (Settings() with { Guest = GuestOs.Windows }).Validate());
        Assert.Throws<InvalidDataException>(() => (Settings() with { ImagePath = "debian.iso" }).Validate());
        (Settings() with { ImagePath = "debian-13-generic-amd64.qcow2" }).Validate();
        Assert.Throws<ArgumentException>(() => new QemuVmHost(Settings() with { Hypervisor = Hypervisor.HyperV }, "log"));
    }

    [Fact, Trait("Pruefung", "SEC-24")]
    public void Qemu_Befehlszeile_ohne_Netzwerkgeraet_mit_seccomp_und_Sockets()
    {
        var host = new QemuVmHost(Settings(), Path.Combine(_dir, "qemu.log"));
        var args = host.BuildArguments(attachSeed: true, Path.Combine(_dir, "vm, mit Komma", "transfer-000001.raw"), "xf1", kvm: false);
        var line = string.Join(' ', args);
        Assert.Equal("none", args[args.ToList().IndexOf("-nic") + 1]);
        Assert.Contains("-nodefaults", args);
        Assert.DoesNotContain(args, a => a.StartsWith("-netdev", StringComparison.Ordinal) || a.Contains("e1000", StringComparison.Ordinal)
                                         || a.Contains("virtio-net", StringComparison.Ordinal) || a == "-vnc" || a == "-spice");
        Assert.Equal("none", args[args.ToList().IndexOf("-display") + 1]);
        Assert.Contains("on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny", args);
        Assert.Contains("max,vmx=off,svm=off", args);
        Assert.Contains("tcg", args);
        // Kommas im Pfad werden für QEMU verdoppelt (sonst Option-Injektion über den Ordnernamen).
        Assert.Contains("vm,, mit Komma", line, StringComparison.Ordinal);
        Assert.DoesNotContain("vm, mit Komma/LocalAi", line, StringComparison.Ordinal);
        Assert.Contains($"name={QemuLink.PortName}", line, StringComparison.Ordinal);
        Assert.Contains("serial=localai-seed", line, StringComparison.Ordinal);
        Assert.Contains("serial=localai-transfer", line, StringComparison.Ordinal);
        Assert.All(args.Where(a => a.Contains(".sock", StringComparison.Ordinal)), a => Assert.Contains(host.SocketDirectory.Replace(",", ",,"), a));
        Assert.True(host.SocketDirectory.Length < 80, "Unix-Socket-Pfade kurz halten");
        var kvm = host.BuildArguments(attachSeed: false, "t.raw", "xf2", kvm: true);
        Assert.Contains("host,vmx=off,svm=off", kvm);
        Assert.DoesNotContain(kvm, a => a.Contains("localai-seed", StringComparison.Ordinal));
        var nested = new QemuVmHost(Settings() with { NestedVirtualization = true }, "log").BuildArguments(false, "t.raw", "xf3", kvm: true);
        Assert.Contains("host", nested);
    }

    [Fact, Trait("Pruefung", "SEC-24")]
    public void Netzwerkgeraete_werden_auch_hinter_Bruecken_gezaehlt()
    {
        var pci = JsonNode.Parse("""
            [ { "bus": 0, "devices": [
                { "class_info": { "class": 1536 } },
                { "class_info": { "class": 256 }, "qdev_id": "xferdev" },
                { "class_info": { "class": 1540 }, "pci_bridge": { "devices": [ { "class_info": { "class": 512 } } ] } },
                { "class_info": { "class": 640 } } ] } ]
            """)!.AsArray();
        Assert.Equal(2, QemuVmHost.CountNetworkDevices(pci));
        Assert.Equal(0, QemuVmHost.CountNetworkDevices([]));
    }

    [Fact, Trait("Pruefung", "M10-Kanal")]
    public void Verbindungskanal_nimmt_nur_feste_Zeilen_an()
    {
        var link = QemuLink.Detached();
        var kvp = new List<(string, string)>();
        var log = new List<string>();
        link.Kvp = (k, v) => kvp.Add((k, v));
        link.Log = log.Add;
        Assert.False(link.GuestReady);
        link.Handle("hello");
        link.Handle("hb");
        Assert.True(link.GuestReady);
        link.Handle("kvp LocalAiSignal\tresult:7");
        link.Handle("kvp böse;Schlüssel\tx");
        link.Handle("kvp OhneTab");
        link.Handle("rm -rf /");
        Assert.Equal([("LocalAiSignal", "result:7")], kvp);
        Assert.Contains(log, l => l.Contains("verworfen", StringComparison.Ordinal));
    }

    [Fact, Trait("Pruefung", "M10-Transfer")]
    public void Transfer_Laufwerk_im_Rohformat_wird_streng_gelesen()
    {
        var limit = 1024L * 1024;
        var empty = Path.Combine(_dir, "leer.raw");
        using (var f = File.Create(empty))
        {
            f.SetLength(limit);
        }
        Assert.Null(TransferContainer.Read(empty, limit));
        var good = Path.Combine(_dir, "gut.raw");
        File.WriteAllBytes(good, TransferContainer.Build(TransferKind.Result, 5, "{\"ok\":true}"u8));
        var message = TransferContainer.Read(good, limit)!;
        Assert.Equal(5, message.Sequence);
        Assert.Equal(TransferKind.Result, message.Kind);
        var tampered = TransferContainer.Build(TransferKind.Result, 5, "{\"ok\":true}"u8);
        tampered[TransferContainer.HeaderSize] ^= 1;
        File.WriteAllBytes(good, tampered);
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(good, limit));
        var big = Path.Combine(_dir, "gross.raw");
        using (var f = File.Create(big))
        {
            f.SetLength(limit + 1);
        }
        Assert.Throws<VhdxFormatException>(() => TransferContainer.Read(big, limit));
    }

    [Fact, Trait("Pruefung", "M10-Gast")]
    public void Gastkonfiguration_und_cloud_config_fuer_qemu()
    {
        var json = JsonNode.Parse(GuestPayloadBuilder.GuestConfigJson(64L << 20, ["python-linux"], true, GuestOs.Linux, null, Hypervisor.Qemu))!;
        Assert.Equal("Qemu", json["hypervisor"]!.GetValue<string>());
        Assert.Equal("HyperV", JsonNode.Parse(GuestPayloadBuilder.GuestConfigJson(1, [], true, GuestOs.Linux, null))!["hypervisor"]!.GetValue<string>());
        Assert.Contains("> /dev/ttyS0", GuestPayloadBuilder.CloudConfig(null, serialConsole: true), StringComparison.Ordinal);
        Assert.DoesNotContain("ttyS0", GuestPayloadBuilder.CloudConfig(null), StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "M10-Gast")]
    public void Linux_Bausteine_brauchen_unter_qemu_kein_hyperv_daemons()
    {
        var dir = Directory.CreateDirectory(Path.Combine(_dir, "linux-gast")).FullName;
        var files = new Dictionary<string, string>
        {
            ["cpython-3.13.9+20251014-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"] = "py",
            ["wheels/pydantic-2.12.0-py3-none-any.whl"] = "a",
            ["wheels/pydantic_core-2.41.0-cp313-cp313-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"] = "b",
            ["wheels/annotated_types-0.7.0-py3-none-any.whl"] = "c",
            ["wheels/typing_extensions-4.15.0-py3-none-any.whl"] = "d",
            ["wheels/typing_inspection-0.4.2-py3-none-any.whl"] = "e",
        };
        foreach (var (name, content) in files)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.Combine(dir, name))!);
            File.WriteAllText(Path.Combine(dir, name), content);
        }
        var manifest = new
        {
            schemaVersion = 1, python = files.Keys.First(), pythonVersion = "3.13.9",
            wheels = files.Keys.Skip(1).ToArray(), debs = Array.Empty<string>(),
            sha256 = files.ToDictionary(f => f.Key, f => Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(f.Value)))),
        };
        File.WriteAllText(Path.Combine(dir, LinuxPrerequisites.ManifestFile), JsonSerializer.Serialize(manifest));
        LinuxPrerequisites.Load(dir, Hypervisor.Qemu);
        var ex = Assert.Throws<InvalidDataException>(() => LinuxPrerequisites.Load(dir));
        Assert.Contains("hyperv-daemons", ex.Message, StringComparison.Ordinal);
    }

    [Fact, Trait("Pruefung", "M10-Konfiguration")]
    public void Vorlage_fuer_den_Linux_Host_ist_gueltig()
    {
        var repo = ContractTests.RepoRoot();
        var options = HostOptions.Parse(["--config", Path.Combine(repo, "config", "localai.linux.json"), "--data", Path.Combine(_dir, "data")]);
        Assert.Equal(SandboxMode.Qemu, options.Mode);
        options.Qemu!.Validate();
        Assert.Equal("Auto", options.Qemu.QemuAccel);
        Assert.True(options.Web!.LowIntegrity, "Webabruf unter Linux in bubblewrap");
        var project = ProjectLoader.Load(Path.Combine(repo, "config", "m10-linux-project.json"), repo);
        Assert.Equal(GuestOs.Linux, project.GuestOs);
    }

    [Fact, Trait("Pruefung", "M10-Konfiguration")]
    public void Linux_Host_lehnt_Windows_Gast_ab()
    {
        var plan = GuestSelection.Resolve(new ProjectConfig(1, "p", "p", [], null, GuestOs.Windows), GuestOs.Linux, _ => null);
        Assert.Contains(GuestOs.Windows, plan.Required);
    }
}
