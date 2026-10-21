using System.Text.Json;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Headless.XUnit;
using Avalonia.LogicalTree;
using Avalonia.Threading;
using Avalonia.VisualTree;
using LocalAi.Contracts;
using LocalAi.Desktop.Core;
using LocalAi.Desktop.Linux;

[assembly: AvaloniaTestApplication(typeof(LocalAi.Desktop.Linux.Tests.TestApp))]

namespace LocalAi.Desktop.Linux.Tests;

public static class TestApp
{
    public static AppBuilder BuildAvaloniaApp() =>
        AppBuilder.Configure<App>().UseHeadless(new AvaloniaHeadlessPlatformOptions()).WithInterFont();
}

/// <summary>M10: Linux-Oberfläche (Avalonia) mit denselben ViewModels wie die WPF-Oberfläche – ohne Bildschirm geprüft.</summary>
public sealed class OberflaecheTests
{
    private static ProposalView Toolchain()
    {
        var now = DateTimeOffset.UtcNow;
        var proposal = new ToolProposal(1, Guid.NewGuid().ToString("D"), "run", "task", AgentRole.Tester, "Toolchain.Test", "1", "Tests ausführen",
            JsonSerializer.SerializeToElement(new { grantId = "g", toolchain = "python-linux", parameters = new { path = "." } }),
            [], "snap", 1, "sbx-qemu", ContractTime.ToContract(now), ContractTime.ToContract(now.AddMinutes(5)));
        return new ProposalView(proposal, ProposalHashing.Sha256(proposal), ProposalState.AwaitingApproval);
    }

    private static async Task<(MainWindow Window, MainViewModel Vm, FakeApi Api)> OpenAsync(string mode = "qemu", params ProposalView[] pending)
    {
        var api = new FakeApi { Pending = [.. pending] };
        var vm = new MainViewModel(api, api, a => Dispatcher.UIThread.Post(a), Guid.NewGuid().ToString("D"), mode);
        var window = new MainWindow { DataContext = vm };
        window.Show();
        await vm.InitializeAsync();
        await Settle();
        return (window, vm, api);
    }

    private static async Task Settle()
    {
        for (var i = 0; i < 5; i++)
        {
            Dispatcher.UIThread.RunJobs();
            await Task.Delay(20);
        }
        Dispatcher.UIThread.RunJobs();
    }

    private static T Find<T>(Control root, Func<T, bool> match) where T : Control =>
        root.GetVisualDescendants().OfType<T>().FirstOrDefault(match)
        ?? root.GetLogicalDescendants().OfType<T>().First(match);

    [AvaloniaFact, Trait("Pruefung", "M10-Oberfläche")]
    public async Task Fenster_laedt_mit_deutschen_Texten_und_VM_Knopf_fuer_qemu()
    {
        var (window, vm, _) = await OpenAsync();
        Assert.Equal(UiText.Get("AppTitle"), window.Title);
        Assert.True(vm.IsHyperV, "qemu ist eine Betriebsart mit Offline-VM");
        var rebuild = Find<Button>(window, b => Equals(b.Content, UiText.Get("RebuildSandbox")));
        Assert.True(rebuild.IsVisible);
        var stop = Find<Button>(window, b => Equals(b.Content, UiText.Get("EmergencyStop")));
        Assert.True(stop.IsEffectivelyVisible);
        window.Close();
    }

    [AvaloniaFact, Trait("Pruefung", "M10-Oberfläche")]
    public async Task Ohne_Verbindung_nur_Statuszeile()
    {
        var window = new MainWindow();
        window.Show();
        window.ShowStatus("Verbindung zum Broker fehlgeschlagen");
        await Settle();
        Assert.DoesNotContain(window.GetVisualDescendants().OfType<Button>(), b => b.IsEffectivelyVisible);
        Assert.Contains(window.GetVisualDescendants().OfType<TextBlock>(), t => t.Text == "Verbindung zum Broker fehlgeschlagen" && t.IsEffectivelyVisible);
        Assert.DoesNotContain(window.GetVisualDescendants().OfType<TextBlock>(),
            t => t.Text == UiText.Get("StoppedBanner") && t.IsEffectivelyVisible);
        window.Close();
    }

    [AvaloniaFact, Trait("Pruefung", "M10-Oberfläche")]
    public async Task Ohne_VM_kein_Neuaufbau_Knopf()
    {
        var (window, _, _) = await OpenAsync("dev-local-unsafe");
        var rebuild = Find<Button>(window, b => Equals(b.Content, UiText.Get("RebuildSandbox")));
        Assert.False(rebuild.IsVisible);
        window.Close();
    }

    [AvaloniaFact, Trait("Pruefung", "AC16")]
    public async Task Freigabekarte_zeigt_Aufruf_und_Fokus_liegt_auf_Ablehnen()
    {
        var (window, vm, _) = await OpenAsync("qemu", Toolchain());
        Assert.Single(vm.Approvals);
        vm.SelectedApproval = vm.Approvals[0];
        await Settle();
        var deny = Find<Button>(window, b => b.Name == "DenyButton");
        Assert.True(deny.IsEffectivelyVisible);
        Assert.True(deny.IsFocused, "Standardfokus auf Ablehnen");
        Assert.False(deny.IsDefault);
        var approve = Find<Button>(window, b => Equals(b.Content, vm.SelectedApproval!.ApproveText));
        Assert.False(approve.IsDefault, "Enter erlaubt nie");
        var command = Find<TextBox>(window, t => t.Text?.Contains("python-linux", StringComparison.Ordinal) == true
                                                 && t.Text.Contains("Aktion", StringComparison.Ordinal));
        Assert.True(command.IsReadOnly);
        window.Close();
    }

    [AvaloniaFact, Trait("Pruefung", "M10-Oberfläche")]
    public async Task Neuaufbau_fragt_asynchron_nach()
    {
        var (window, vm, api) = await OpenAsync();
        vm.ConfirmRebuildAsync = () => Task.FromResult(false);
        vm.RebuildSandboxCommand.Execute(null);
        await Settle();
        Assert.Equal(0, api.Rebuilds);
        vm.ConfirmRebuildAsync = () => Task.FromResult(true);
        vm.RebuildSandboxCommand.Execute(null);
        await Settle();
        Assert.Equal(1, api.Rebuilds);
        window.Close();
    }

    [AvaloniaFact, Trait("Pruefung", "M10-Oberfläche")]
    public async Task Rueckfrage_ist_standardmaessig_Nein()
    {
        var owner = new Window();
        owner.Show();
        var answer = ConfirmDialog.AskAsync(owner, "Titel", "Wirklich?");
        await Settle();
        var dialog = owner.OwnedWindows.OfType<ConfirmDialog>().Single();
        var no = Find<Button>(dialog, b => Equals(b.Content, UiText.Get("ConfirmNo")));
        Assert.True(no.IsFocused);
        Assert.True(no.IsCancel);
        dialog.Close();
        await Settle();
        Assert.False(await answer);
        owner.Close();
    }

    private sealed class FakeApi : IBrokerUiApi
    {
        public List<ProposalView> Pending { get; init; } = [];

        public int Rebuilds { get; private set; }

        public Task<IReadOnlyList<ProposalView>> PendingAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<ProposalView>>(Pending);
        public Task<ProposalView> DecideAsync(UserDecision decision, CancellationToken ct) => throw new NotSupportedException();
        public Task<ProposalView> NarrowAsync(NarrowRequest request, CancellationToken ct) => throw new NotSupportedException();
        public async Task<IReadOnlyList<BrokerEvent>> WaitEventsAsync(long afterSeq, int timeoutMs, CancellationToken ct)
        {
            await Task.Delay(Math.Min(timeoutMs, 200), ct);
            return [];
        }
        public Task EmergencyStopAsync(CancellationToken ct) => Task.CompletedTask;
        public Task ReleaseAsync(CancellationToken ct) => Task.CompletedTask;
        public Task<JsonElement> StartFakeAgentAsync(CancellationToken ct) => throw new NotSupportedException();
        public Task<IReadOnlyList<FolderGrant>> GrantsAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<FolderGrant>>([]);
        public Task<JsonElement> StatusAsync(CancellationToken ct) =>
            Task.FromResult(JsonDocument.Parse("""{"sandbox":"Offline-VM bereit"}""").RootElement.Clone());
        public Task<IReadOnlyList<ToolchainInfo>> ScanToolchainsAsync(CancellationToken ct) => Task.FromResult<IReadOnlyList<ToolchainInfo>>([]);
        public Task RegisterToolchainAsync(string directory, string digest, CancellationToken ct) => Task.CompletedTask;
        public Task UnregisterToolchainAsync(string name, CancellationToken ct) => Task.CompletedTask;
        public Task<string> ChatSendAsync(string text, CancellationToken ct) => Task.FromResult("run-1");
        public Task AgentCancelAsync(string agent, CancellationToken ct) => Task.CompletedTask;
        public Task RunCancelAsync(CancellationToken ct) => Task.CompletedTask;
        public async Task<IReadOnlyList<ActivityEvent>> WaitActivityAsync(long afterSeq, int timeoutMs, CancellationToken ct)
        {
            await Task.Delay(Math.Min(timeoutMs, 200), ct);
            return [];
        }
        public Task OrchestratorStopAsync(CancellationToken ct) => Task.CompletedTask;
        public Task RebuildSandboxAsync(CancellationToken ct)
        {
            Rebuilds++;
            return Task.CompletedTask;
        }
    }
}
