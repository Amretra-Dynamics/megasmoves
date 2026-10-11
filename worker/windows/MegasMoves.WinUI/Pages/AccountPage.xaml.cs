using System.ComponentModel;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using MegasMoves.ViewModels;

namespace MegasMoves.Pages;

public sealed partial class AccountPage : Page, INotifyPropertyChanged
{
    public MainWindowViewModel ViewModel { get; }

    public Visibility SignInVisibility => ViewModel.AccountAuthenticated ? Visibility.Collapsed : Visibility.Visible;
    public Visibility SignOutVisibility => ViewModel.AccountAuthenticated ? Visibility.Visible : Visibility.Collapsed;

    public event PropertyChangedEventHandler? PropertyChanged;

    public AccountPage()
    {
        ViewModel = App.Services.GetRequiredService<MainWindowViewModel>();
        InitializeComponent();
        ViewModel.PropertyChanged += ViewModel_PropertyChanged;
        Unloaded += (_, _) => ViewModel.PropertyChanged -= ViewModel_PropertyChanged;
    }

    private void ViewModel_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(MainWindowViewModel.AccountAuthenticated))
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(SignInVisibility)));
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(SignOutVisibility)));
        }
    }
}
