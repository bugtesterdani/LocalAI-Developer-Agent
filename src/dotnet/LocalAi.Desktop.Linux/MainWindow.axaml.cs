using Avalonia.Controls;
using Avalonia.Markup.Xaml;

namespace LocalAi.Desktop.Linux;

public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        AvaloniaXamlLoader.Load(this);
    }

    /// <summary>Statuszeile vor dem Verbinden (noch ohne ViewModel).</summary>
    public void ShowStatus(string text)
    {
        if (this.FindControl<TextBlock>("StartupStatus") is { } status)
        {
            status.Text = text;
        }
    }
}
