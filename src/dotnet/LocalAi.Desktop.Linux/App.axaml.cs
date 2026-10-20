using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;
using Avalonia.Threading;
using LocalAi.Desktop.Core;

namespace LocalAi.Desktop.Linux;

/// <summary>
/// Linux-Oberfläche (M10): startet den Broker neben der Anwendung, verbindet sich dreimal (Anfragen, Ereignisse,
/// Live-Aktivität) und zeigt dieselben ViewModels wie die WPF-Oberfläche unter Windows.
/// </summary>
public sealed class App : Application
{
    private BrokerProcess? _broker;
    private BrokerConnection? _requests;
    private BrokerConnection? _events;
    private BrokerConnection? _activity;
    private MainViewModel? _viewModel;
    private bool _closed;

    public override void Initialize() => AvaloniaXamlLoader.Load(this);

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop)
        {
            var window = new MainWindow();
            desktop.MainWindow = window;
            window.Closing += OnClosing;
            Dispatcher.UIThread.Post(() => _ = ConnectAsync(window));
        }
        base.OnFrameworkInitializationCompleted();
    }

    private async Task ConnectAsync(MainWindow window)
    {
        window.ShowStatus(UiText.Get("Starting"));
        try
        {
            _broker = await BrokerProcess.StartAsync(CancellationToken.None);
            var hs = _broker.Handshake;
            _requests = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
            _events = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
            _activity = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
            _viewModel = new MainViewModel(_requests, _events, action => Dispatcher.UIThread.Post(action), hs.SessionId, hs.Mode, _activity)
            {
                ConfirmLicenseAsync = info => ConfirmDialog.AskAsync(window, UiText.Get("ToolchainLicenseTitle"),
                    UiText.Format("ToolchainLicenseConfirm", info.DisplayName, info.LicenseNote ?? "")),
                ConfirmRebuildAsync = () => ConfirmDialog.AskAsync(window, UiText.Get("RebuildSandbox"), UiText.Get("SandboxRebuildConfirm")),
            };
            window.DataContext = _viewModel;
            await _viewModel.InitializeAsync();
            window.ShowStatus(string.Empty);
        }
        catch (Exception ex)
        {
            window.ShowStatus(UiText.Format("ConnectFailed", ex.Message));
        }
    }

    /// <summary>Beim Schließen erst Broker (samt VM) sauber beenden, dann das Fenster schließen.</summary>
    private async void OnClosing(object? sender, WindowClosingEventArgs e)
    {
        if (_closed || sender is not Window window)
        {
            return;
        }
        e.Cancel = true;
        window.IsEnabled = false;
        if (window is MainWindow main)
        {
            main.ShowStatus(UiText.Get("Stopping"));
        }
        try
        {
            if (_viewModel is not null)
            {
                await _viewModel.DisposeAsync();
            }
            foreach (var connection in new[] { _events, _activity, _requests })
            {
                if (connection is not null)
                {
                    await connection.DisposeAsync();
                }
            }
            if (_broker is not null)
            {
                await _broker.DisposeAsync();
            }
        }
        finally
        {
            _closed = true;
            window.Close();
        }
    }
}
