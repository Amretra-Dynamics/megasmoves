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

public sealed partial class ActivityPage : Page
{
    private CancellationTokenSource? _followCancellation;
    private bool _loaded;

    public ActivityViewModel ViewModel { get; }

    public ActivityPage()
    {
        ViewModel = App.Services.GetRequiredService<ActivityViewModel>();
        InitializeComponent();
        Loaded += ActivityPage_Loaded;
        Unloaded += ActivityPage_Unloaded;
    }

    private async void ActivityPage_Loaded(object sender, RoutedEventArgs e)
    {
        _loaded = true;
        await ViewModel.RefreshAsync();
        if (FollowToggle.IsOn)
        {
            StartFollow();
        }
    }

    private void ActivityPage_Unloaded(object sender, RoutedEventArgs e)
    {
        _loaded = false;
        StopFollow();
    }

    private void FollowToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (!_loaded)
        {
            return;
        }
        if (FollowToggle.IsOn)
        {
            StartFollow();
        }
        else
        {
            StopFollow();
        }
    }

    private void StartFollow()
    {
        StopFollow();
        _followCancellation = new CancellationTokenSource();
        _ = RunFollowAsync(_followCancellation.Token);
    }

    private void StopFollow()
    {
        var cancellation = Interlocked.Exchange(ref _followCancellation, null);
        if (cancellation is null)
        {
            return;
        }
        cancellation.Cancel();
        cancellation.Dispose();
    }

    private async Task RunFollowAsync(CancellationToken cancellationToken)
    {
        try
        {
            await ViewModel.FollowAsync(cancellationToken);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
    }

    private async Task ApplyFiltersAsync(bool clear)
    {
        if (clear)
        {
            await ViewModel.ClearFiltersAsync();
        }
        else
        {
            await ViewModel.RefreshAsync();
        }
        if (FollowToggle.IsOn)
        {
            StartFollow();
        }
    }

    private async void QueryBox_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == VirtualKey.Enter)
        {
            e.Handled = true;
            await ApplyFiltersAsync(clear: false);
        }
    }

    private async void ApplyFilters_Click(object sender, RoutedEventArgs e) =>
        await ApplyFiltersAsync(clear: false);

    private async void ClearFilters_Click(object sender, RoutedEventArgs e) =>
        await ApplyFiltersAsync(clear: true);

    private void CopySelected_Click(object sender, RoutedEventArgs e)
    {
        if (ActivityList.SelectedItem is not ActivityItemViewModel row)
        {
            return;
        }
        var package = new DataPackage();
        package.SetText(ViewModel.CopyText(row));
        Clipboard.SetContent(package);
        Clipboard.Flush();
    }

    private async void Export_Click(object sender, RoutedEventArgs e)
    {
        var window = App.Services.GetRequiredService<MainWindow>();
        var picker = new FileSavePicker
        {
            SuggestedFileName = $"MegasMoves-Activity-{DateTimeOffset.Now:yyyyMMdd-HHmmss}"
        };
        picker.FileTypeChoices.Add("JSON", new List<string> { ".json" });
        WinRT.Interop.InitializeWithWindow.Initialize(
            picker,
            WinRT.Interop.WindowNative.GetWindowHandle(window));

        StorageFile? file = await picker.PickSaveFileAsync();
        if (file is null)
        {
            return;
        }
        await FileIO.WriteTextAsync(file, ViewModel.ExportJson());
    }
}
