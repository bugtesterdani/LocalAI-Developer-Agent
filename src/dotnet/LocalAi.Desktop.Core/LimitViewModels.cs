using System.Globalization;
using LocalAi.Contracts;

namespace LocalAi.Desktop.Core;

/// <summary>
/// Rückfrage „Schrittlimit erreicht“: zeigt die vollständige Eingabe des nächsten Schritts (Regeln, Aufgabe, Verlauf und
/// Evidenz) bearbeitbar an. Weitermachen sendet genau diesen Text beim nächsten Schritt; die Aufgabe gilt danach für alle
/// weiteren Schritte. Beenden lässt die Aufgabe wie bisher am Limit enden.
/// </summary>
public sealed class LimitQuestionViewModel : ObservableObject
{
    private readonly Func<LimitAnswer, Task> _answer;
    private string _systemText;
    private string _taskText;
    private string _restText;
    private string _extraStepsText;
    private bool _busy;

    public LimitQuestionViewModel(LimitQuestion question, Func<LimitAnswer, Task> answer)
    {
        Question = question;
        _answer = answer;
        _systemText = question.System;
        _taskText = question.Task;
        _restText = question.Rest;
        _extraStepsText = question.SuggestedSteps.ToString(CultureInfo.InvariantCulture);
        ContinueCommand = new RelayCommand(() => SendAsync(LimitAction.Continue), () => CanContinue);
        StopCommand = new RelayCommand(() => SendAsync(LimitAction.Stop), () => !_busy);
        ResetCommand = new RelayCommand(ResetAsync, () => IsChanged && !_busy);
    }

    public LimitQuestion Question { get; }
    public string QuestionId => Question.QuestionId;

    public string HeaderText => UiText.Format("LimitHeader", UiText.AgentName(Question.Agent), Question.TaskId,
        InertText.Clean(Question.Title, 200), Question.Steps);

    public string SystemText
    {
        get => _systemText;
        set => SetText(ref _systemText, value);
    }

    /// <summary>Aufgabe und Anforderungen – eine Änderung gilt für alle weiteren Schritte.</summary>
    public string TaskText
    {
        get => _taskText;
        set => SetText(ref _taskText, value);
    }

    /// <summary>Verlauf und Evidenz – eine Änderung gilt nur für den nächsten Schritt.</summary>
    public string RestText
    {
        get => _restText;
        set => SetText(ref _restText, value);
    }

    public string ExtraStepsText
    {
        get => _extraStepsText;
        set
        {
            if (Set(ref _extraStepsText, value))
            {
                OnPropertyChanged(nameof(ExtraStepsError));
                Refresh();
            }
        }
    }

    public int? ExtraSteps => int.TryParse(_extraStepsText.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out var n)
                              && n is >= 1 and <= 200 ? n : null;

    public string ExtraStepsError => ExtraSteps is null ? UiText.Get("LimitStepsInvalid") : string.Empty;

    public bool IsChanged => _systemText != Question.System || _taskText != Question.Task || _restText != Question.Rest;

    public bool IsTaskChanged => _taskText.Trim() != Question.Task.Trim();

    public string ChangeText => !IsChanged
        ? UiText.Get("LimitUnchanged")
        : UiText.Get(IsTaskChanged ? "LimitTaskChanged" : "LimitInputChanged");

    public string SizeText => UiText.Format("LimitSize", _systemText.Length + _taskText.Length + _restText.Length);

    public bool CanContinue => !_busy && ExtraSteps is not null && !string.IsNullOrWhiteSpace(_taskText);

    public RelayCommand ContinueCommand { get; }
    public RelayCommand StopCommand { get; }
    public RelayCommand ResetCommand { get; }

    /// <summary>Antwort, wie sie an den Broker geht (für Tests sichtbar).</summary>
    public LimitAnswer BuildAnswer(LimitAction action) => action == LimitAction.Continue
        ? new LimitAnswer(QuestionId, action, ExtraSteps, _systemText, _taskText, _restText)
        : new LimitAnswer(QuestionId, action);

    private async Task SendAsync(LimitAction action)
    {
        _busy = true;
        Refresh();
        try
        {
            await _answer(BuildAnswer(action)).ConfigureAwait(true);
        }
        finally
        {
            _busy = false;
            Refresh();
        }
    }

    private Task ResetAsync()
    {
        SystemText = Question.System;
        TaskText = Question.Task;
        RestText = Question.Rest;
        return Task.CompletedTask;
    }

    private void SetText(ref string field, string value, [System.Runtime.CompilerServices.CallerMemberName] string? name = null)
    {
        if (Set(ref field, value ?? string.Empty, name))
        {
            OnPropertyChanged(nameof(IsChanged));
            OnPropertyChanged(nameof(ChangeText));
            OnPropertyChanged(nameof(SizeText));
            Refresh();
        }
    }

    private void Refresh()
    {
        OnPropertyChanged(nameof(CanContinue));
        ContinueCommand.Refresh();
        StopCommand.Refresh();
        ResetCommand.Refresh();
    }
}
