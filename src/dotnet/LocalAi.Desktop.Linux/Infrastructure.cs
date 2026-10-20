using System.Collections.Specialized;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Markup.Xaml;
using LocalAi.Desktop.Core;

namespace LocalAi.Desktop.Linux;

/// <summary>XAML-Zugriff auf deutsche Oberflächentexte: <c>{loc:T Schlüssel}</c> (wie in der WPF-Oberfläche).</summary>
public sealed class TExtension : MarkupExtension
{
    public TExtension()
    {
    }

    public TExtension(string key)
    {
        Key = key;
    }

    public string Key { get; set; } = string.Empty;

    public override object ProvideValue(IServiceProvider serviceProvider) => UiText.Get(Key);
}

/// <summary>
/// Hält Live-Ausgaben und Listen am Ende, solange der Nutzer nicht gerade darin arbeitet (Tastaturfokus = Nutzer liest
/// oder markiert: dann nicht springen).
/// </summary>
public sealed class AutoScroll : AvaloniaObject
{
    public static readonly AttachedProperty<bool> ToEndProperty =
        AvaloniaProperty.RegisterAttached<AutoScroll, Control, bool>("ToEnd");

    static AutoScroll()
    {
        ToEndProperty.Changed.AddClassHandler<Control>(OnChanged);
    }

    public static bool GetToEnd(Control element) => element.GetValue(ToEndProperty);

    public static void SetToEnd(Control element, bool value) => element.SetValue(ToEndProperty, value);

    private static void OnChanged(Control control, AvaloniaPropertyChangedEventArgs e)
    {
        if (e.NewValue is not true)
        {
            return;
        }
        switch (control)
        {
            case TextBox box:
                box.PropertyChanged += (_, args) =>
                {
                    if (args.Property == TextBox.TextProperty && !box.IsKeyboardFocusWithin)
                    {
                        box.CaretIndex = box.Text?.Length ?? 0;
                    }
                };
                break;
            case ListBox list:
                if (list.Items is INotifyCollectionChanged items)
                {
                    items.CollectionChanged += (_, args) =>
                    {
                        if (args.Action == NotifyCollectionChangedAction.Add && list.ItemCount > 0 && !list.IsKeyboardFocusWithin)
                        {
                            list.ScrollIntoView(list.ItemCount - 1);
                        }
                    };
                }
                break;
        }
    }
}

/// <summary>Setzt den Tastaturfokus beim Anzeigen (Freigabekarte: Fokus auf „Ablehnen“, Enter erlaubt nie).</summary>
public sealed class FocusOnShow : AvaloniaObject
{
    public static readonly AttachedProperty<bool> EnabledProperty =
        AvaloniaProperty.RegisterAttached<FocusOnShow, Control, bool>("Enabled");

    static FocusOnShow()
    {
        EnabledProperty.Changed.AddClassHandler<Control>((control, e) =>
        {
            if (e.NewValue is true)
            {
                control.AttachedToVisualTree += (_, _) => control.Focus();
            }
        });
    }

    public static bool GetEnabled(Control element) => element.GetValue(EnabledProperty);

    public static void SetEnabled(Control element, bool value) => element.SetValue(EnabledProperty, value);
}

/// <summary>Ja/Nein-Rückfrage als modales Fenster; Standard ist „Nein“ (Fokus, Escape).</summary>
public sealed class ConfirmDialog : Window
{
    private ConfirmDialog(string title, string message)
    {
        Title = title;
        Width = 520;
        SizeToContent = SizeToContent.Height;
        CanResize = false;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        var no = new Button { Content = UiText.Get("ConfirmNo"), IsCancel = true, Padding = new Thickness(14, 6), Margin = new Thickness(0, 0, 8, 0) };
        var yes = new Button { Content = UiText.Get("ConfirmYes"), Padding = new Thickness(14, 6) };
        no.Click += (_, _) => Close(false);
        yes.Click += (_, _) => Close(true);
        Content = new StackPanel
        {
            Margin = new Thickness(16),
            Children =
            {
                new SelectableTextBlock { Text = message, TextWrapping = Avalonia.Media.TextWrapping.Wrap },
                new StackPanel
                {
                    Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 16, 0, 0),
                    Children = { no, yes },
                },
            },
        };
        Opened += (_, _) => no.Focus();
    }

    public static async Task<bool> AskAsync(Window owner, string title, string message) =>
        await new ConfirmDialog(title, message).ShowDialog<bool>(owner);
}
