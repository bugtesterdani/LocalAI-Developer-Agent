using System.Collections.Specialized;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Markup;
using LocalAi.Desktop.Core;

namespace LocalAi.Desktop;

/// <summary>XAML-Zugriff auf deutsche Oberflächentexte: <c>{loc:T Schlüssel}</c>.</summary>
[MarkupExtensionReturnType(typeof(string))]
public sealed class TExtension : MarkupExtension
{
    public TExtension()
    {
    }

    public TExtension(string key)
    {
        Key = key;
    }

    [ConstructorArgument("key")]
    public string Key { get; set; } = string.Empty;

    public override object ProvideValue(IServiceProvider serviceProvider) => UiText.Get(Key);
}

/// <summary>true → Collapsed, false → Visible.</summary>
public sealed class InverseBoolToVisibilityConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture) =>
        value is true ? Visibility.Collapsed : Visibility.Visible;

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) =>
        throw new NotSupportedException();
}

/// <summary>null oder leer → Collapsed.</summary>
public sealed class EmptyToCollapsedConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture) =>
        value is null || value is string { Length: 0 } ? Visibility.Collapsed : Visibility.Visible;

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) =>
        throw new NotSupportedException();
}

/// <summary>
/// Hält Live-Ausgaben und Listen am Ende, solange der Nutzer nicht gerade darin arbeitet
/// (Tastaturfokus im Textfeld = Nutzer liest oder markiert: dann nicht springen).
/// </summary>
public static class AutoScroll
{
    public static readonly DependencyProperty ToEndProperty = DependencyProperty.RegisterAttached(
        "ToEnd", typeof(bool), typeof(AutoScroll), new PropertyMetadata(false, OnChanged));

    public static bool GetToEnd(DependencyObject element) => (bool)element.GetValue(ToEndProperty);

    public static void SetToEnd(DependencyObject element, bool value) => element.SetValue(ToEndProperty, value);

    private static void OnChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (e.NewValue is not true)
        {
            return;
        }
        switch (d)
        {
            case TextBox box:
                box.TextChanged += (_, _) =>
                {
                    if (!box.IsKeyboardFocusWithin)
                    {
                        box.ScrollToEnd();
                    }
                };
                break;
            case ListBox list:
                ((INotifyCollectionChanged)list.Items).CollectionChanged += (_, args) =>
                {
                    if (args.Action == NotifyCollectionChangedAction.Add && list.Items.Count > 0 && !list.IsKeyboardFocusWithin)
                    {
                        list.ScrollIntoView(list.Items[^1]);
                    }
                };
                break;
        }
    }
}
