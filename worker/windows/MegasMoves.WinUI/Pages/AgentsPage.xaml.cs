using Microsoft.Extensions.DependencyInjection;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using MegasMoves.ViewModels;

namespace MegasMoves.Pages;

public sealed partial class AgentsPage : Page
{
    private bool _initialized;

    public AgentsViewModel ViewModel { get; }
    public Visibility NoTasksVisibility => ViewModel.HasTasks ? Visibility.Collapsed : Visibility.Visible;

    public AgentsPage()
    {
        ViewModel = App.Services.GetRequiredService<AgentsViewModel>();
        InitializeComponent();
        Loaded += OnLoaded;
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        if (_initialized)
        {
            return;
        }
        _initialized = true;
        await ViewModel.InitializeAsync();
    }

    private void AgentInstallClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: AgentWorkbenchItemViewModel agent })
        {
            ViewModel.InstallCommand.Execute(agent);
        }
    }

    private void AgentConnectClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: AgentWorkbenchItemViewModel agent })
        {
            ViewModel.ConnectCommand.Execute(agent);
        }
    }

    private void AgentDisconnectClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: AgentWorkbenchItemViewModel agent })
        {
            ViewModel.DisconnectCommand.Execute(agent);
        }
    }

    private void AgentUninstallClicked(object sender, RoutedEventArgs e)
    {
        if (sender is Button { DataContext: AgentWorkbenchItemViewModel agent })
        {
            ViewModel.UninstallCommand.Execute(agent);
        }
    }
}
