using LocalAi.Contracts;

namespace LocalAi.Broker;

/// <summary>
/// Offene Rückfragen „Schrittlimit erreicht“ (Orchestrator → Oberfläche). Eine je Agent und Aufgabe; eine neue Frage
/// derselben Aufgabe ersetzt die alte. Die Antwort der Oberfläche geht als Auftrag <c>LimitAnswer</c> an den Orchestrator.
/// Keine Freigabe: Der Inhalt ist Text für das Modell, nie ein Werkzeugaufruf.
/// </summary>
public sealed class LimitQuestionStore(IClock clock)
{
    /// <summary>Höchstlänge je Textfeld (Regeln, Aufgabe, Verlauf/Evidenz) – ein Prompt mit 256K Token passt hinein.</summary>
    public const int MaxFieldChars = 2_000_000;
    public const int MaxExtraSteps = 200;
    private const int MaxOpen = 20;

    private readonly Lock _gate = new();
    private readonly List<LimitQuestion> _open = [];

    public LimitQuestion Add(LimitQuestion question)
    {
        Require(question.QuestionId, "questionId", 100);
        Require(question.RunId, "runId", 100);
        Require(question.Agent, "agent", 50);
        Require(question.TaskId, "taskId", 100);
        if (question.Title.Length > 500)
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, "title zu lang");
        }
        foreach (var (name, text) in new[] { ("system", question.System), ("task", question.Task), ("rest", question.Rest) })
        {
            if (text.Length > MaxFieldChars)
            {
                throw new BrokerException(BrokerErrorCodes.Unprocessable, $"{name} länger als {MaxFieldChars} Zeichen");
            }
        }
        if (question.Steps < 1 || question.SuggestedSteps < 1 || question.SuggestedSteps > MaxExtraSteps)
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, "steps bzw. suggestedSteps ungültig");
        }
        var stored = question with { CreatedAt = ContractTime.ToContract(clock.UtcNow) };
        lock (_gate)
        {
            _open.RemoveAll(q => q.QuestionId == question.QuestionId
                                 || (q.RunId == question.RunId && q.Agent == question.Agent && q.TaskId == question.TaskId));
            if (_open.Count >= MaxOpen)
            {
                throw new BrokerException(BrokerErrorCodes.TooMany, "Zu viele offene Rückfragen");
            }
            _open.Add(stored);
        }
        return stored;
    }

    public IReadOnlyList<LimitQuestion> Pending()
    {
        lock (_gate)
        {
            return [.. _open];
        }
    }

    /// <summary>Prüft die Antwort und entfernt die Frage (eine Antwort je Frage).</summary>
    public LimitQuestion Answer(LimitAnswer answer)
    {
        lock (_gate)
        {
            var question = _open.FirstOrDefault(q => q.QuestionId == answer.QuestionId)
                           ?? throw new BrokerException(BrokerErrorCodes.Gone, "Die Rückfrage ist nicht mehr offen (beantwortet, abgebrochen oder Run beendet)");
            if (!Enum.IsDefined(answer.Action))
            {
                throw new BrokerException(BrokerErrorCodes.Unprocessable, "Unbekannte Aktion");
            }
            if (answer.Action == LimitAction.Continue)
            {
                if (answer.ExtraSteps is not { } extra || extra < 1 || extra > MaxExtraSteps)
                {
                    throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Weitere Schritte: 1 bis {MaxExtraSteps}");
                }
                if (answer.System is null || answer.Task is null || answer.Rest is null)
                {
                    throw new BrokerException(BrokerErrorCodes.Unprocessable, "Eingabe des nächsten Schritts fehlt");
                }
                if (string.IsNullOrWhiteSpace(answer.Task))
                {
                    throw new BrokerException(BrokerErrorCodes.Unprocessable, "Die Aufgabe darf nicht leer sein");
                }
                if (answer.System.Length > MaxFieldChars || answer.Task.Length > MaxFieldChars || answer.Rest.Length > MaxFieldChars)
                {
                    throw new BrokerException(BrokerErrorCodes.Unprocessable, $"Eingabe länger als {MaxFieldChars} Zeichen je Feld");
                }
            }
            _open.Remove(question);
            return question;
        }
    }

    /// <summary>Frage zurückziehen (Agent abgebrochen, Run beendet).</summary>
    public bool Withdraw(string questionId)
    {
        lock (_gate)
        {
            return _open.RemoveAll(q => q.QuestionId == questionId) > 0;
        }
    }

    /// <summary>Fragen eines Agenten verwerfen (Abbruch des Agenten).</summary>
    public int ClearAgent(string? runId, string agent)
    {
        lock (_gate)
        {
            return runId is null ? 0 : _open.RemoveAll(q => q.RunId == runId && q.Agent == agent);
        }
    }

    /// <summary>Alle Fragen eines Runs verwerfen (Orchestrator beendet oder Run abgebrochen).</summary>
    public int ClearRun(string? runId)
    {
        lock (_gate)
        {
            return runId is null ? 0 : _open.RemoveAll(q => q.RunId == runId);
        }
    }

    private static void Require(string? value, string name, int max)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > max)
        {
            throw new BrokerException(BrokerErrorCodes.Unprocessable, $"{name} fehlt oder ist zu lang");
        }
    }
}
