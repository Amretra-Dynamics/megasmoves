using Microsoft.Extensions.DependencyInjection;
using Microsoft.UI.Xaml.Controls;
using MegasMoves.ViewModels;

namespace MegasMoves.Pages;

public sealed partial class TestsPage : Page
{
    public MainWindowViewModel ViewModel { get; }

    public TestsPage()
    {
        ViewModel = App.Services.GetRequiredService<MainWindowViewModel>();
        InitializeComponent();
    }
}
