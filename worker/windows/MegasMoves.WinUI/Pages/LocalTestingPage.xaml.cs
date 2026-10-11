using System.ComponentModel;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using MegasMoves.ViewModels;
using Windows.Storage.Pickers;

namespace MegasMoves.Pages;

public sealed partial class LocalTestingPage : Page, INotifyPropertyChanged
{
    private bool _initialized;
    private bool _subscribed;

    public LocalTestingViewModel ViewModel { get; }

    public Visibility EmptyStateVisibility => ViewModel.HasProjects ? Visibility.Collapsed : Visibility.Visible;
    public Visibility TargetEmptyVisibility => ViewModel.HasProjects && !ViewModel.HasTargets ? Visibility.Visible : Visibility.Collapsed;
    public Visibility ServiceEmptyVisibility => ViewModel.HasProjects && !ViewModel.HasServices ? Visibility.Visible : Visibility.Collapsed;
    public Visibility DeviceEmptyVisibility => ViewModel.HasDevices ? Visibility.Collapsed : Visibility.Visible;

    public event PropertyChangedEventHandler? PropertyChanged;

    public LocalTestingPage()
    {
        ViewModel = App.Services.GetRequiredService<LocalTestingViewModel>();
        InitializeComponent();
        Loaded += OnLoaded;
        Unloaded += OnUnloaded;
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        if (!_subscribed)
        {
            ViewModel.PropertyChanged += ViewModel_PropertyChanged;
            _subscribed = true;
        }
        if (!_initialized)
        {
            _initialized = true;
            await ViewModel.InitializeAsync();
        }
    }

    private void OnUnloaded(object sender, RoutedEventArgs e)
    {
        if (_subscribed)
        {
            ViewModel.PropertyChanged -= ViewModel_PropertyChanged;
            _subscribed = false;
        }
    }

    private void ViewModel_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName is nameof(LocalTestingViewModel.HasProjects)
            or nameof(LocalTestingViewModel.HasTargets)
            or nameof(LocalTestingViewModel.HasServices)
            or nameof(LocalTestingViewModel.HasDevices))
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(EmptyStateVisibility)));
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(TargetEmptyVisibility)));
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(ServiceEmptyVisibility)));
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(DeviceEmptyVisibility)));
        }
    }

    private async void AddFolder_Click(object sender, RoutedEventArgs e)
    {
        var window = App.Services.GetRequiredService<MainWindow>();
        var picker = new FolderPicker();
        picker.FileTypeFilter.Add("*");
        WinRT.Interop.InitializeWithWindow.Initialize(
            picker,
            WinRT.Interop.WindowNative.GetWindowHandle(window));

        var folder = await picker.PickSingleFolderAsync();
        if (folder is null)
        {
            return;
        }
        await ViewModel.AddTrustedFolderCommand.ExecuteAsync(folder.Path);
    }

    private void ProjectCheckClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: LocalTestingProjectItemViewModel project })
        {
            ViewModel.CheckProjectCommand.Execute(project);
        }
    }

    private void TargetBuildClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: LocalTestingTargetItemViewModel target })
        {
            ViewModel.BuildTargetCommand.Execute(target);
        }
    }

    private void TargetOpenClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: LocalTestingTargetItemViewModel target })
        {
            ViewModel.OpenTargetCommand.Execute(target);
        }
    }

    private void TargetStopClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: LocalTestingTargetItemViewModel target })
        {
            ViewModel.StopTargetCommand.Execute(target);
        }
    }

    private void TargetLogsClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: LocalTestingTargetItemViewModel target })
        {
            ViewModel.CaptureTargetLogsCommand.Execute(target);
        }
    }

    private void ServiceStartClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: LocalTestingServiceItemViewModel service })
        {
            ViewModel.StartServiceCommand.Execute(service);
        }
    }

    private void ServiceRestartClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: LocalTestingServiceItemViewModel service })
        {
            ViewModel.RestartServiceCommand.Execute(service);
        }
    }

    private void ServiceStopClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: LocalTestingServiceItemViewModel service })
        {
            ViewModel.StopServiceCommand.Execute(service);
        }
    }

    private void ServiceLogsClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: LocalTestingServiceItemViewModel service })
        {
            ViewModel.CaptureServiceLogsCommand.Execute(service);
        }
    }
}
