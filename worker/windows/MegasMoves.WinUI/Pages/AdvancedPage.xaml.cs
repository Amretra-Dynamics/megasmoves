using Microsoft.Extensions.DependencyInjection;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using MegasMoves.ViewModels;
using Windows.ApplicationModel.DataTransfer;
using Windows.Storage;
using Windows.Storage.Pickers;
using Windows.System;

namespace MegasMoves.Pages;

public sealed partial class AdvancedPage : Page
{
    private CancellationTokenSource? _rawFollowCancellation;

    public MainWindowViewModel Main { get; }
    public DiagnosticsViewModel Diagnostics { get; }

    public AdvancedPage()
    {
        Main = App.Services.GetRequiredService<MainWindowViewModel>();
        Diagnostics = App.Services.GetRequiredService<DiagnosticsViewModel>();
        InitializeComponent();

        Loaded += AdvancedPage_Loaded;
        Unloaded += AdvancedPage_Unloaded;
    }

    private void AdvancedPage_Loaded(object sender, RoutedEventArgs e)
    {
        if (ServicePicker.SelectedItem is null && Main.RuntimeServices.Count > 0)
        {
            ServicePicker.SelectedIndex = 0;
        }
    }

    private void AdvancedPage_Unloaded(object sender, RoutedEventArgs e)
    {
        StopRawFollow();
    }

    private async void LoadLogs_Click(object sender, RoutedEventArgs e)
    {
        if (ServicePicker.SelectedItem is RuntimeServiceViewModel service)
        {
            await Diagnostics.LoadRawLogsAsync(service);
        }
    }

    private void RawFollowToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (RawFollowToggle.IsOn)
        {
            StartRawFollow();
        }
        else
        {
            StopRawFollow();
        }
    }

    private void ServicePicker_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!RawFollowToggle.IsOn)
        {
            return;
        }
        StopRawFollow();
        StartRawFollow();
    }

    private void StartRawFollow()
    {
        if (ServicePicker.SelectedItem is not RuntimeServiceViewModel service)
        {
            RawFollowToggle.IsOn = false;
            return;
        }

        StopRawFollow();
        _rawFollowCancellation = new CancellationTokenSource();
        var cancellation = _rawFollowCancellation;
        _ = FollowRawLogsAsync(service, cancellation);
    }

    private async Task FollowRawLogsAsync(
        RuntimeServiceViewModel service,
        CancellationTokenSource cancellation)
    {
        try
        {
            await Diagnostics.FollowRawLogsAsync(service, cancellation.Token);
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
        }
        finally
        {
            if (ReferenceEquals(_rawFollowCancellation, cancellation))
            {
                _rawFollowCancellation = null;
            }
            cancellation.Dispose();
        }
    }

    private void StopRawFollow()
    {
        var current = Interlocked.Exchange(ref _rawFollowCancellation, null);
        if (current is null)
        {
            return;
        }
        try
        {
            current.Cancel();
        }
        catch (ObjectDisposedException)
        {
        }
    }

    private void RawSearchBox_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == VirtualKey.Enter)
        {
            e.Handled = true;
            Diagnostics.ApplySearch();
        }
    }

    private void ClearRawSearch_Click(object sender, RoutedEventArgs e)
    {
        Diagnostics.ClearSearch();
    }

    private void CopyRawLogs_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrEmpty(Diagnostics.DisplayedRawLogText))
        {
            return;
        }
        var package = new DataPackage();
        package.SetText(Diagnostics.DisplayedRawLogText);
        Clipboard.SetContent(package);
        Clipboard.Flush();
    }

    private async void ExportRawLogs_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrEmpty(Diagnostics.DisplayedRawLogText))
        {
            return;
        }

        var window = App.Services.GetRequiredService<MainWindow>();
        var picker = new FileSavePicker
        {
            SuggestedFileName = $"MegasMoves-{SafeFileName(Diagnostics.RawLogTitle)}-{DateTimeOffset.Now:yyyyMMdd-HHmmss}"
        };
        picker.FileTypeChoices.Add("Log text", new List<string> { ".log" });
        WinRT.Interop.InitializeWithWindow.Initialize(
            picker,
            WinRT.Interop.WindowNative.GetWindowHandle(window));

        StorageFile? file = await picker.PickSaveFileAsync();
        if (file is null)
        {
            return;
        }
        await FileIO.WriteTextAsync(file, Diagnostics.DisplayedRawLogText);
    }

    private static string SafeFileName(string value)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var cleaned = new string(value.Select(ch => invalid.Contains(ch) ? '-' : ch).ToArray());
        return string.IsNullOrWhiteSpace(cleaned) ? "logs" : cleaned;
    }
}
