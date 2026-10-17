using System.Globalization;
using LocalAi.Contracts;

namespace LocalAi.Desktop.Core;

/// <summary>Ein Toolchain-Ordner bzw. eine Registrierung in der Oberfläche.</summary>
public sealed class ToolchainItemViewModel
{
    public ToolchainItemViewModel(ToolchainInfo info, MainViewModel owner)
    {
        Info = info;
        RegisterCommand = new RelayCommand(() => owner.RegisterToolchainAsync(this), () => CanRegister && !owner.IsStopped);
        UnregisterCommand = new RelayCommand(() => owner.UnregisterToolchainAsync(this), () => Info.Registered);
    }

    public ToolchainInfo Info { get; }

    public RelayCommand RegisterCommand { get; }

    public RelayCommand UnregisterCommand { get; }

    public string Name => Info.Name;

    public bool CanRegister => !Info.Registered && Info.Problems.Count == 0;

    public string TitleText => $"{Info.Name} {Info.Version} – {Info.DisplayName}";

    public string StatusText
    {
        get
        {
            var parts = new List<string> { UiText.Get(Info.Registered ? "ToolchainRegistered" : "ToolchainNotRegistered") };
            if (Info.UsedByProject)
            {
                parts.Add(UiText.Get("ToolchainUsedByProject"));
            }
            if (Info.Proprietary)
            {
                parts.Add(UiText.Format("ToolchainLicense", Info.LicenseNote ?? ""));
            }
            return string.Join(" · ", parts);
        }
    }

    public string DetailText => UiText.Format("ToolchainDetails", Info.FileCount, FormatSize(Info.TotalBytes),
        Info.Digest.Length >= 16 ? Info.Digest[..16] : Info.Digest, Info.Directory);

    public string? ProblemsText => Info.Problems.Count == 0 ? null : string.Join(Environment.NewLine, Info.Problems.Select(p => "• " + p));

    public void RefreshCommands()
    {
        RegisterCommand.Refresh();
        UnregisterCommand.Refresh();
    }

    public static string FormatSize(long bytes) => bytes switch
    {
        >= 1L << 30 => (bytes / (double)(1L << 30)).ToString("F1", UiText.Culture) + " GiB",
        >= 1L << 20 => (bytes / (double)(1L << 20)).ToString("F1", UiText.Culture) + " MiB",
        >= 1L << 10 => (bytes / 1024.0).ToString("F0", UiText.Culture) + " KiB",
        _ => bytes.ToString(CultureInfo.InvariantCulture) + " B",
    };
}
