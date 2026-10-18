using System.Windows;
using LocalAi.Desktop.Core;

namespace LocalAi.Desktop;

public partial class App : Application
{
    private BrokerProcess? _broker;
    private BrokerConnection? _requests;
    private BrokerConnection? _events;
    private BrokerConnection? _activity;
    private MainViewModel? _viewModel;

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var window = new MainWindow();
        MainWindow = window;
        window.Show();
        window.ShowStatus(UiText.Get("Starting"));

        try
        {
            _broker = await BrokerProcess.StartAsync(CancellationToken.None);
            var hs = _broker.Handshake;
            // Zwei Verbindungen: eine für Anfragen, eine für das Warten auf Ereignisse.
            _requests = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
            _events = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
            // Dritte Verbindung nur für die Live-Aktivität: lange Abfragen blockieren weder Ereignisse noch Befehle.
            _activity = await BrokerConnection.ConnectAsync(hs.Pipe, hs.UiToken, CancellationToken.None);
            _viewModel = new MainViewModel(_requests, _events, action => Dispatcher.BeginInvoke(action), hs.SessionId, hs.Mode, _activity);
            _viewModel.ConfirmLicense = info => MessageBox.Show(window,
                UiText.Format("ToolchainLicenseConfirm", info.DisplayName, info.LicenseNote ?? ""),
                UiText.Get("ToolchainLicenseTitle"), MessageBoxButton.YesNo, MessageBoxImage.Question, MessageBoxResult.No) == MessageBoxResult.Yes;
            _viewModel.ConfirmRebuild = () => MessageBox.Show(window, UiText.Get("SandboxRebuildConfirm"), UiText.Get("RebuildSandbox"),
                MessageBoxButton.YesNo, MessageBoxImage.Question, MessageBoxResult.No) == MessageBoxResult.Yes;
            window.DataContext = _viewModel;
            await _viewModel.InitializeAsync();
        }
        catch (Exception ex)
        {
            window.ShowStatus(UiText.Format("ConnectFailed", ex.Message));
        }
    }

    protected override async void OnExit(ExitEventArgs e)
    {
        if (_viewModel is not null)
        {
            await _viewModel.DisposeAsync();
        }
        if (_events is not null)
        {
            await _events.DisposeAsync();
        }
        if (_activity is not null)
        {
            await _activity.DisposeAsync();
        }
        if (_requests is not null)
        {
            await _requests.DisposeAsync();
        }
        if (_broker is not null)
        {
            await _broker.DisposeAsync();
        }
        base.OnExit(e);
    }
}
