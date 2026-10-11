using System.Collections.ObjectModel;
using System.Collections.Specialized;
using System.ComponentModel;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using MegasMoves.ViewModels;
using Windows.Storage.Pickers;

namespace MegasMoves.Pages;

public sealed partial class RuntimePage : Page, INotifyPropertyChanged
{
    private ProjectCardViewModel? _selectedProject;

    public MainWindowViewModel ViewModel { get; }
    public ObservableCollection<ProjectCardViewModel> FilteredProjects { get; } = [];
    public ObservableCollection<RuntimeServiceViewModel> SelectedServices { get; } = [];

    public Visibility EmptyStateVisibility => ViewModel.HasProjects ? Visibility.Collapsed : Visibility.Visible;
    public Visibility ProjectDetailVisibility => SelectedProject is null ? Visibility.Collapsed : Visibility.Visible;
    public Visibility SelectionPromptVisibility => ViewModel.HasProjects && SelectedProject is null
        ? Visibility.Visible
        : Visibility.Collapsed;

    public ProjectCardViewModel? SelectedProject
    {
        get => _selectedProject;
        private set
        {
            if (ReferenceEquals(_selectedProject, value))
            {
                return;
            }
            if (_selectedProject is not null)
            {
                _selectedProject.PropertyChanged -= SelectedProject_PropertyChanged;
            }
            _selectedProject = value;
            if (_selectedProject is not null)
            {
                _selectedProject.PropertyChanged += SelectedProject_PropertyChanged;
            }
            RefreshSelectedServices();
            NotifySelectionProperties();
        }
    }

    public string SelectedProjectName => SelectedProject?.Name ?? string.Empty;
    public string SelectedProjectRoot => SelectedProject?.Root ?? string.Empty;
    public string SelectedProjectState => SelectedProject?.State ?? "Unavailable";
    public string SelectedTargetSummary => SelectedProject?.TargetSummary ?? "No runnable targets";
    public string SelectedServiceSummary => SelectedProject?.ServiceSummary ?? "No runtime service";
    public string SelectedTargetLabel => string.IsNullOrWhiteSpace(SelectedProject?.TargetLabel)
        ? "No preferred target"
        : SelectedProject!.TargetLabel;
    public string SelectedTargetKind => string.IsNullOrWhiteSpace(SelectedProject?.TargetKind)
        ? "none"
        : SelectedProject!.TargetKind;
    public string SelectedServiceName => string.IsNullOrWhiteSpace(SelectedProject?.Service)
        ? "No declared service"
        : SelectedProject!.Service;
    public string SelectedLastAction => SelectedProject?.LastAction ?? "Ready";

    public event PropertyChangedEventHandler? PropertyChanged;

    public RuntimePage()
    {
        ViewModel = App.Services.GetRequiredService<MainWindowViewModel>();
        InitializeComponent();
        ViewModel.PropertyChanged += ViewModel_PropertyChanged;
        ViewModel.Projects.CollectionChanged += Projects_CollectionChanged;
        ViewModel.RuntimeServices.CollectionChanged += RuntimeServices_CollectionChanged;
        Loaded += RuntimePage_Loaded;
        Unloaded += RuntimePage_Unloaded;
    }

    private void RuntimePage_Loaded(object sender, RoutedEventArgs e)
    {
        RefreshProjectList();
    }

    private void RuntimePage_Unloaded(object sender, RoutedEventArgs e)
    {
        ViewModel.PropertyChanged -= ViewModel_PropertyChanged;
        ViewModel.Projects.CollectionChanged -= Projects_CollectionChanged;
        ViewModel.RuntimeServices.CollectionChanged -= RuntimeServices_CollectionChanged;
        if (_selectedProject is not null)
        {
            _selectedProject.PropertyChanged -= SelectedProject_PropertyChanged;
        }
    }

    private void ViewModel_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(MainWindowViewModel.HasProjects))
        {
            Notify(nameof(EmptyStateVisibility));
            Notify(nameof(SelectionPromptVisibility));
        }
    }

    private void Projects_CollectionChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        RefreshProjectList();
    }

    private void RuntimeServices_CollectionChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        RefreshSelectedServices();
    }

    private void SelectedProject_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(ProjectCardViewModel.LastAction))
        {
            Notify(nameof(SelectedLastAction));
        }
    }

    private void ProjectFilterBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        RefreshProjectList();
    }

    private void ProjectPicker_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        SelectedProject = ProjectPicker.SelectedItem as ProjectCardViewModel;
    }

    private void RefreshProjectList()
    {
        var previousName = SelectedProject?.Name;
        var needle = ProjectFilterBox?.Text?.Trim() ?? string.Empty;
        var candidates = ViewModel.Projects
            .Where(project =>
                string.IsNullOrWhiteSpace(needle) ||
                project.Name.Contains(needle, StringComparison.OrdinalIgnoreCase) ||
                project.Root.Contains(needle, StringComparison.OrdinalIgnoreCase) ||
                project.TargetKind.Contains(needle, StringComparison.OrdinalIgnoreCase) ||
                project.Service.Contains(needle, StringComparison.OrdinalIgnoreCase))
            .OrderBy(project => project.Name, StringComparer.OrdinalIgnoreCase)
            .ToArray();

        FilteredProjects.Clear();
        foreach (var project in candidates)
        {
            FilteredProjects.Add(project);
        }

        var selected = candidates.FirstOrDefault(project =>
            string.Equals(project.Name, previousName, StringComparison.OrdinalIgnoreCase));
        selected ??= candidates.FirstOrDefault();
        SelectedProject = selected;
        if (ProjectPicker is not null)
        {
            ProjectPicker.SelectedItem = selected;
        }

        Notify(nameof(EmptyStateVisibility));
        Notify(nameof(SelectionPromptVisibility));
        Notify(nameof(ProjectDetailVisibility));
    }

    private void RefreshSelectedServices()
    {
        SelectedServices.Clear();
        if (SelectedProject is null)
        {
            return;
        }
        foreach (var service in ViewModel.RuntimeServices.Where(service =>
                     string.Equals(service.Project, SelectedProject.Name, StringComparison.OrdinalIgnoreCase)))
        {
            SelectedServices.Add(service);
        }
    }

    private async void OpenSelected_Click(object sender, RoutedEventArgs e)
    {
        await ExecuteSelectedAsync(SelectedProject?.OpenCommand);
    }

    private async void BuildSelected_Click(object sender, RoutedEventArgs e)
    {
        await ExecuteSelectedAsync(SelectedProject?.BuildCommand);
    }

    private async void CheckSelected_Click(object sender, RoutedEventArgs e)
    {
        await ExecuteSelectedAsync(SelectedProject?.CheckCommand);
    }

    private async void StopSelected_Click(object sender, RoutedEventArgs e)
    {
        await ExecuteSelectedAsync(SelectedProject?.StopCommand);
    }

    private async Task ExecuteSelectedAsync(CommunityToolkit.Mvvm.Input.IAsyncRelayCommand? command)
    {
        if (command is null || !command.CanExecute(null))
        {
            return;
        }
        await command.ExecuteAsync(null);
        Notify(nameof(SelectedLastAction));
        if (ViewModel.RefreshCommand.CanExecute(null))
        {
            await ViewModel.RefreshCommand.ExecuteAsync(null);
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

    private void NewTerminal_Click(object sender, RoutedEventArgs e)
    {
        NewTerminalPanel.Visibility = Visibility.Visible;
        NewTerminalCommandBox.Focus(FocusState.Programmatic);
    }

    private void CancelNewTerminal_Click(object sender, RoutedEventArgs e)
    {
        NewTerminalPanel.Visibility = Visibility.Collapsed;
        NewTerminalCommandBox.Text = string.Empty;
    }

    private async void SpawnTerminal_Click(object sender, RoutedEventArgs e)
    {
        var cmd = NewTerminalCommandBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(cmd)) return;
        NewTerminalPanel.Visibility = Visibility.Collapsed;
        NewTerminalCommandBox.Text = string.Empty;
        await ViewModel.CreateTerminalAsync(cmd);
    }

    private void NotifySelectionProperties()
    {
        Notify(nameof(SelectedProject));
        Notify(nameof(SelectedProjectName));
        Notify(nameof(SelectedProjectRoot));
        Notify(nameof(SelectedProjectState));
        Notify(nameof(SelectedTargetSummary));
        Notify(nameof(SelectedServiceSummary));
        Notify(nameof(SelectedTargetLabel));
        Notify(nameof(SelectedTargetKind));
        Notify(nameof(SelectedServiceName));
        Notify(nameof(SelectedLastAction));
        Notify(nameof(ProjectDetailVisibility));
        Notify(nameof(SelectionPromptVisibility));
    }

    private void Notify(string propertyName)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
    }
}
