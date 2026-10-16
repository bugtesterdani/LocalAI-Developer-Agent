using System.Diagnostics;
using System.Reflection;

namespace LocalAi.Tests;

/// <summary>Markiert eine Testmethode (öffentlich, parameterlos, void oder Task).</summary>
[AttributeUsage(AttributeTargets.Method)]
public sealed class TestAttribute(string? id = null) : Attribute
{
    public string? Id { get; } = id;
}

public sealed class AssertionException(string message) : Exception(message);

public static class Assert
{
    public static void True(bool condition, string message)
    {
        if (!condition)
        {
            throw new AssertionException(message);
        }
    }

    public static void Equal<T>(T expected, T actual, string? message = null)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual))
        {
            throw new AssertionException($"{message ?? "Werte verschieden"}: erwartet <{expected}>, erhalten <{actual}>");
        }
    }

    public static TException Throws<TException>(Action action, string? message = null) where TException : Exception
    {
        try
        {
            action();
        }
        catch (TException ex)
        {
            return ex;
        }
        catch (Exception ex)
        {
            throw new AssertionException($"{message ?? "Falsche Ausnahme"}: erwartet {typeof(TException).Name}, erhalten {ex.GetType().Name}: {ex.Message}");
        }
        throw new AssertionException($"{message ?? "Keine Ausnahme"}: erwartet {typeof(TException).Name}");
    }
}

/// <summary>
/// Minimaler Testrunner, solange NuGet (xUnit) in der Build-Umgebung nicht erreichbar ist.
/// Aufruf: dotnet run --project src/dotnet/LocalAi.Tests [-- Filter]
/// </summary>
public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        var filter = args.FirstOrDefault();
        var tests = typeof(Program).Assembly.GetTypes()
            .SelectMany(t => t.GetMethods(BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static)
                .Select(m => (Type: t, Method: m, Attr: m.GetCustomAttribute<TestAttribute>())))
            .Where(x => x.Attr is not null)
            .Where(x => filter is null || $"{x.Type.Name}.{x.Method.Name} {x.Attr!.Id}".Contains(filter, StringComparison.OrdinalIgnoreCase))
            .OrderBy(x => x.Type.Name).ThenBy(x => x.Method.Name)
            .ToList();

        var failed = 0;
        var total = Stopwatch.StartNew();
        foreach (var (type, method, attr) in tests)
        {
            var name = $"{type.Name}.{method.Name}" + (attr!.Id is null ? string.Empty : $" [{attr.Id}]");
            var sw = Stopwatch.StartNew();
            try
            {
                var instance = method.IsStatic ? null : Activator.CreateInstance(type);
                var result = method.Invoke(instance, null);
                if (result is Task task)
                {
                    await task.WaitAsync(TimeSpan.FromSeconds(60));
                }
                if (instance is IAsyncDisposable ad)
                {
                    await ad.DisposeAsync();
                }
                else if (instance is IDisposable d)
                {
                    d.Dispose();
                }
                Console.WriteLine($"  OK    {name} ({sw.ElapsedMilliseconds} ms)");
            }
            catch (Exception ex)
            {
                failed++;
                var inner = ex is TargetInvocationException { InnerException: { } ie } ? ie : ex;
                Console.WriteLine($"  FEHLER {name}: {inner.GetType().Name}: {inner.Message}");
            }
        }
        Console.WriteLine();
        Console.WriteLine($"{tests.Count - failed} von {tests.Count} Tests bestanden ({total.ElapsedMilliseconds} ms).");
        return failed == 0 ? 0 : 1;
    }
}
