using System.ComponentModel;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using MegasMoves.ViewModels;

namespace MegasMoves.Pages;

public sealed partial class OverviewPage : Page, INotifyPropertyChanged
{
    public MainWindowViewModel ViewModel { get; }
    public Visibility AttentionVisibility => ViewModel.HasAttention ? Visibility.Visible : Visibility.Collapsed;
    public Visibility NothingNeedsAttentionVisibility => ViewModel.HasAttention ? Visibility.Collapsed : Visibility.Visible;
    public Visibility NoFailuresVisibility => (ViewModel.FailureCount == 0 && ViewModel.LatestFailures.Count == 0) ? Visibility.Visible : Visibility.Collapsed;
    public Visibility NoActivityVisibility => ViewModel.Activity.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    public Visibility NoProjectsVisibility => ViewModel.HasProjects ? Visibility.Collapsed : Visibility.Visible;

    public event PropertyChangedEventHandler? PropertyChanged;

    public OverviewPage()
    {
        ViewModel = App.Services.GetRequiredService<MainWindowViewModel>();
        InitializeComponent();
        ViewModel.PropertyChanged += ViewModel_PropertyChanged;
        ViewModel.Activity.CollectionChanged += (_, _) =>
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(NoActivityVisibility)));
        };
        Unloaded += (_, _) => ViewModel.PropertyChanged -= ViewModel_PropertyChanged;
    }

    private void ViewModel_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(MainWindowViewModel.HasAttention) || e.PropertyName == nameof(MainWindowViewModel.NeedsYouCount))
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(AttentionVisibility)));
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(NothingNeedsAttentionVisibility)));
        }
        if (e.PropertyName == nameof(MainWindowViewModel.FailureCount) || e.PropertyName == nameof(MainWindowViewModel.LatestFailures))
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(NoFailuresVisibility)));
        }
        if (e.PropertyName == nameof(MainWindowViewModel.HasProjects) || e.PropertyName == nameof(MainWindowViewModel.Projects))
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(NoProjectsVisibility)));
        }
    }

    private void ViewAllActivity_Click(object sender, RoutedEventArgs e)
    {
        Frame?.Navigate(typeof(ActivityPage));
    }
}
