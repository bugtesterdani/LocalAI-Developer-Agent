using System.Globalization;
using System.Resources;
using LocalAi.Contracts;

namespace LocalAi.Desktop.Core;

/// <summary>Oberflächentexte aus Resources/UiText.resx (Deutsch).</summary>
public static class UiText
{
    private static readonly ResourceManager Resources = new("LocalAi.Desktop.Core.Resources.UiText", typeof(UiText).Assembly);

    public static CultureInfo Culture { get; set; } = CultureInfo.GetCultureInfo("de-DE");

    public static string Get(string key) =>
        Resources.GetString(key, Culture) ?? $"[{key}]";

    public static bool Exists(string key) => Resources.GetString(key, Culture) is not null;

    public static string Format(string key, params object?[] args) =>
        string.Format(Culture, Get(key), args);

    public static string State(ProposalState state) => Get($"State_{state}");

    public static string AgentState(AgentState state) => Get($"AgentState_{state}");

    /// <summary>Anzeigename eines Absenders im Chat (Rollennamen bleiben, Nutzer/Orchestrator deutsch).</summary>
    public static string AgentName(string agent) => Exists($"Sender_{agent}") ? Get($"Sender_{agent}") : agent;

    public static string Event(string type) => Exists($"Event_{type}") ? Get($"Event_{type}") : type;
}
