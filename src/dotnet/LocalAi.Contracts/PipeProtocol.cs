using System.Buffers.Binary;

namespace LocalAi.Contracts;

/// <summary>
/// Rahmenprotokoll über Named Pipes: 4 Byte Länge (Little Endian) + UTF-8-JSON.
/// Anfrage: {"id":n,"method":"...","token":"...","params":{...}}
/// Antwort: {"id":n,"ok":true,"result":...} oder {"id":n,"ok":false,"error":{"code":403,"message":"..."}}
/// Gleiches Format in Python: <c>localai/pipe.py</c>.
/// </summary>
public static class PipeProtocol
{
    public const int MaxFrameBytes = 8 * 1024 * 1024;

    public static class Methods
    {
        public const string Ping = "ping";
        public const string Status = "status";
        public const string Submit = "proposals.submit";
        public const string Outcome = "proposals.outcome";
        public const string Pending = "proposals.pending";
        public const string Get = "proposals.get";
        public const string Decide = "approvals.decide";
        public const string Narrow = "approvals.narrow";
        public const string WaitEvents = "events.wait";
        public const string EmergencyStop = "run.emergencyStop";
        public const string Release = "run.release";
        public const string StartFakeAgent = "dev.startFakeAgent";
        public const string ProjectGrants = "project.grants";
        public const string ToolchainsScan = "toolchains.scan";
        public const string ToolchainsRegister = "toolchains.register";
        public const string ToolchainsUnregister = "toolchains.unregister";
        public const string ToolchainsAvailable = "toolchains.available";
        public const string ChatSend = "chat.send";
        public const string AgentCancel = "agent.cancel";
        public const string RunCancel = "run.cancel";
        public const string ActivityWait = "activity.wait";
        public const string ActivityReport = "activity.report";
        public const string OrchestratorWait = "orchestrator.wait";
        public const string OrchestratorStatus = "orchestrator.status";
        public const string OrchestratorStop = "orchestrator.stop";
        public const string SandboxRebuild = "sandbox.rebuild";
        public const string SandboxContinue = "sandbox.continue";

        /// <summary>Schrittlimit erreicht: Orchestrator legt die Eingabe des nächsten Schritts zur Entscheidung vor.</summary>
        public const string LimitAsk = "limit.ask";
        public const string LimitWithdraw = "limit.withdraw";
        public const string LimitPending = "limit.pending";
        public const string LimitAnswer = "limit.answer";

        /// <summary>Sanitisierter Text eines Webabrufs für die Inhaltsfreigabe – nur die Oberfläche (SEC-09).</summary>
        public const string WebCapture = "web.capture";
        public const string SystemManifest = "system.manifest";
        public const string AuditVerify = "audit.verify";
        public const string DiagnosticsExport = "diagnostics.export";
        public const string BackupCreate = "backup.create";
    }

    public static async Task<byte[]?> ReadFrameAsync(Stream stream, CancellationToken cancellationToken)
    {
        var header = new byte[4];
        if (!await ReadExactlyOrEofAsync(stream, header, cancellationToken).ConfigureAwait(false))
        {
            return null;
        }
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length < 0 || length > MaxFrameBytes)
        {
            throw new InvalidDataException($"Ungültige Rahmenlänge: {length}");
        }
        var payload = new byte[length];
        if (!await ReadExactlyOrEofAsync(stream, payload, cancellationToken).ConfigureAwait(false))
        {
            throw new EndOfStreamException("Verbindung während eines Rahmens geschlossen");
        }
        return payload;
    }

    public static async Task WriteFrameAsync(Stream stream, ReadOnlyMemory<byte> payload, CancellationToken cancellationToken)
    {
        if (payload.Length > MaxFrameBytes)
        {
            throw new InvalidDataException("Rahmen zu groß");
        }
        var frame = new byte[4 + payload.Length];
        BinaryPrimitives.WriteInt32LittleEndian(frame, payload.Length);
        payload.CopyTo(frame.AsMemory(4));
        await stream.WriteAsync(frame, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    private static async Task<bool> ReadExactlyOrEofAsync(Stream stream, byte[] buffer, CancellationToken cancellationToken)
    {
        var read = 0;
        while (read < buffer.Length)
        {
            var n = await stream.ReadAsync(buffer.AsMemory(read), cancellationToken).ConfigureAwait(false);
            if (n == 0)
            {
                if (read == 0)
                {
                    return false;
                }
                throw new EndOfStreamException("Verbindung während eines Rahmens geschlossen");
            }
            read += n;
        }
        return true;
    }
}
