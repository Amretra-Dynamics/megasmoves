using Microsoft.Extensions.DependencyInjection;
using Microsoft.UI.Xaml.Controls;
using MegasMoves.ViewModels;

namespace MegasMoves.Pages;

public sealed partial class DevicesPage : Page
{
    public MainWindowViewModel ViewModel { get; }

    public DevicesPage()
    {
        ViewModel = App.Services.GetRequiredService<MainWindowViewModel>();
        InitializeComponent();
    }
}
