using System.Windows;

namespace LocalAi.Desktop;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
    }

    /// <summary>Statuszeile vor dem Verbinden (noch ohne ViewModel).</summary>
    public void ShowStatus(string text) => StartupStatus.Text = text;
}
