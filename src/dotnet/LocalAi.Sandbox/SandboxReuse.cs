using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace LocalAi.Sandbox;

/// <summary>
/// Zustand einer wiederverwendbaren Sandbox-VM (Option <c>reuseVm</c>): Fingerabdruck des Aufbaus, Sitzung
/// (Kennung, Schlüssel, letzte Sequenz) und das angehängte Transfer-Laufwerk. Nur eine „saubere“ VM
/// (bei Programmende bereit, nie gesperrt) wird wiederverwendet. <c>fingerprint</c> ist die Grundlage (Abbild,
/// Einstellungen, Laufzeit, Toolchains), <c>codeFingerprint</c> der Worker-Code und die Gastskripte;
/// <c>guestUpdate</c> ≥ 1 heißt, der Gast kann Code-Updates ohne Neuaufbau übernehmen.
/// </summary>
public sealed record SandboxReuseState(
    [property: JsonPropertyName("schemaVersion")] int SchemaVersion,
    [property: JsonPropertyName("fingerprint")] string Fingerprint,
    [property: JsonPropertyName("sessionId")] string SessionId,
    [property: JsonPropertyName("key")] string Key,
    [property: JsonPropertyName("protection")] string Protection,
    [property: JsonPropertyName("lastSequence")] long LastSequence,
    [property: JsonPropertyName("currentDisk")] string CurrentDisk,
    [property: JsonPropertyName("diskCounter")] int DiskCounter,
    [property: JsonPropertyName("builtAt")] DateTimeOffset BuiltAt,
    [property: JsonPropertyName("clean")] bool Clean,
    [property: JsonPropertyName("codeFingerprint")] string? CodeFingerprint = null,
    [property: JsonPropertyName("guestUpdate")] int GuestUpdate = 0);

public sealed class SandboxReuseStore(string vmDirectory)
{
    private static readonly JsonSerializerOptions Options = new() { WriteIndented = true };

    public string FilePath => Path.Combine(vmDirectory, "sandbox-state.json");

    /// <summary>Liest den Zustand; unlesbar oder manipuliert → null (dann wird neu aufgebaut).</summary>
    public (SandboxReuseState State, byte[] Key)? Load()
    {
        try
        {
            if (!File.Exists(FilePath))
            {
                return null;
            }
            var state = JsonSerializer.Deserialize<SandboxReuseState>(File.ReadAllText(FilePath), Options);
            if (state is null || state.SchemaVersion != 1)
            {
                return null;
            }
            var key = KeyProtection.Unprotect(state.Key, state.Protection);
            return key.Length >= 32 ? (state, key) : null;
        }
        catch (Exception ex) when (ex is IOException or JsonException or CryptographicException or FormatException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    public void Save(SandboxReuseState state, byte[] key)
    {
        var (protectedKey, protection) = KeyProtection.Protect(key);
        var tmp = FilePath + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(state with { Key = protectedKey, Protection = protection }, Options), new UTF8Encoding(false));
        if (!OperatingSystem.IsWindows())
        {
            // Ohne DPAPI (Linux-Host, M10): Schlüssel nur für den eigenen Benutzer lesbar.
            File.SetUnixFileMode(tmp, UnixFileMode.UserRead | UnixFileMode.UserWrite);
        }
        File.Move(tmp, FilePath, overwrite: true);
    }

    public void Delete()
    {
        try
        {
            File.Delete(FilePath);
        }
        catch (IOException)
        {
        }
    }
}

/// <summary>Sitzungsschlüssel auf der Platte: unter Windows mit DPAPI an das Benutzerkonto gebunden.</summary>
public static class KeyProtection
{
    public static (string Value, string Protection) Protect(byte[] key)
    {
        if (OperatingSystem.IsWindows())
        {
            return (Convert.ToBase64String(Dpapi(key, protect: true)), "dpapi-user");
        }
        return (Convert.ToHexStringLower(key), "none");
    }

    public static byte[] Unprotect(string value, string protection) => protection switch
    {
        "dpapi-user" when OperatingSystem.IsWindows() => Dpapi(Convert.FromBase64String(value), protect: false),
        "none" => Convert.FromHexString(value),
        _ => throw new CryptographicException($"Schlüsselschutz {protection} nicht verfügbar"),
    };

    [StructLayout(LayoutKind.Sequential)]
    private struct DataBlob
    {
        public int Size;
        public IntPtr Data;
    }

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CryptProtectData(ref DataBlob input, string? description, IntPtr entropy, IntPtr reserved, IntPtr prompt, int flags, ref DataBlob output);

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool CryptUnprotectData(ref DataBlob input, IntPtr description, IntPtr entropy, IntPtr reserved, IntPtr prompt, int flags, ref DataBlob output);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);

    private static byte[] Dpapi(byte[] data, bool protect)
    {
        const int uiForbidden = 0x1;
        var handle = GCHandle.Alloc(data, GCHandleType.Pinned);
        try
        {
            var input = new DataBlob { Size = data.Length, Data = handle.AddrOfPinnedObject() };
            var output = new DataBlob();
            var ok = protect
                ? CryptProtectData(ref input, "LocalAi Sandbox", IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, uiForbidden, ref output)
                : CryptUnprotectData(ref input, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, uiForbidden, ref output);
            if (!ok)
            {
                throw new CryptographicException($"DPAPI fehlgeschlagen (Fehler {Marshal.GetLastWin32Error()})");
            }
            try
            {
                var result = new byte[output.Size];
                Marshal.Copy(output.Data, result, 0, output.Size);
                return result;
            }
            finally
            {
                LocalFree(output.Data);
            }
        }
        finally
        {
            handle.Free();
        }
    }
}
