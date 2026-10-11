using Microsoft.Extensions.DependencyInjection;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using MegasMoves.Services;
using MegasMoves.ViewModels;
using Windows.Storage;
using Windows.Storage.Pickers;

namespace MegasMoves.Pages;

public sealed partial class SettingsPage : Page
{
    private readonly UpdateCoordinator _updates;
    private readonly UpdateApplyLauncher _apply;
    private readonly PerformanceBudgetMonitor _performance;
    private readonly ThemeService _themeService;
    private readonly UiStateStore _uiState;
    private bool _loadingUpdatePolicy;
    private bool _loadingTheme;
    private bool _loadingCloseBehavior;
    private StagedUpdate? _stagedUpdate;

    public MainWindowViewModel ViewModel { get; }

    public SettingsPage()
    {
        ViewModel = App.Services.GetRequiredService<MainWindowViewModel>();
        _updates = App.Services.GetRequiredService<UpdateCoordinator>();
        _apply = App.Services.GetRequiredService<UpdateApplyLauncher>();
        _performance = App.Services.GetRequiredService<PerformanceBudgetMonitor>();
        _themeService = App.Services.GetRequiredService<ThemeService>();
        _uiState = App.Services.GetRequiredService<UiStateStore>();
        InitializeComponent();
        LoadThemePreference();
        LoadCloseBehavior();
        LoadUpdatePolicy();
        RenderPerformance();
    }

    private void LoadThemePreference()
    {
        _loadingTheme = true;
        try
        {
            var mode = _themeService.CurrentMode;
            ThemeRadioButtons.SelectedIndex = mode switch
            {
                AppThemeMode.Light => 1,
                AppThemeMode.Dark => 2,
                _ => 0,
            };
        }
        finally
        {
            _loadingTheme = false;
        }
    }

    private void ThemeRadioButtons_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingTheme || ThemeRadioButtons.SelectedItem is not RadioButton selected)
        {
            return;
        }

        var mode = selected.Tag?.ToString() switch
        {
            "light" => AppThemeMode.Light,
            "dark" => AppThemeMode.Dark,
            _ => AppThemeMode.System,
        };

        _themeService.SetTheme(mode);
    }

    private void LoadCloseBehavior()
    {
        _loadingCloseBehavior = true;
        try
        {
            var behavior = _uiState.GetString("window.close_behavior") ?? "dock";
            CloseBehaviorToggle.IsOn = !string.Equals(behavior, "exit", StringComparison.OrdinalIgnoreCase);
        }
        finally
        {
            _loadingCloseBehavior = false;
        }
    }

    private void CloseBehaviorToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_loadingCloseBehavior)
        {
            return;
        }
        var behavior = CloseBehaviorToggle.IsOn ? "dock" : "exit";
        _uiState.SetString("window.close_behavior", behavior);
    }

    private void QuitAppButton_Click(object sender, RoutedEventArgs e)
    {
        var mainWindow = App.Services.GetRequiredService<MainWindow>();
        mainWindow.ForceClose();
    }

    private void LoadUpdatePolicy()
    {
        _loadingUpdatePolicy = true;
        try
        {
            var policy = _updates.Policy;
            UpdateChannelBox.SelectedIndex = policy.Channel switch
            {
                UpdateChannel.Internal => 1,
                UpdateChannel.Dev => 2,
                _ => 0,
            };
            AutomaticChecksToggle.IsOn = policy.AutomaticChecksEnabled;
            RenderUpdatePolicy(policy);
        }
        finally
        {
            _loadingUpdatePolicy = false;
        }
    }

    private void UpdatePolicy_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingUpdatePolicy || UpdateChannelBox.SelectedItem is not ComboBoxItem selected)
        {
            return;
        }
        var channel = selected.Tag?.ToString() switch
        {
            "internal" => UpdateChannel.Internal,
            "dev" => UpdateChannel.Dev,
            _ => UpdateChannel.Stable,
        };
        _updates.SetPolicy(channel, AutomaticChecksToggle.IsOn);
        RenderUpdatePolicy(_updates.Policy);

        // A manifest is cryptographically bound to its channel. Changing policy
        // invalidates any prior staged selection in the UI rather than carrying it
        // across channels accidentally.
        _stagedUpdate = null;
        ApplyStagedUpdateButton.IsEnabled = false;
        StagedUpdateText.Text = "No update staged for the selected channel.";
    }

    private const string AutomaticInstallStatus = "automatic install: off";

    private void RenderUpdatePolicy(UpdatePolicy policy)
    {
        UpdatePolicyText.Text =
            $"Channel: {UpdateCoordinator.ChannelName(policy.Channel)} · automatic checks: {(policy.AutomaticChecksEnabled ? "on" : "off")} · {AutomaticInstallStatus}";
    }

    private async void UpdateSafety_Click(object sender, RoutedEventArgs e)
    {
        UpdateSafetyBar.IsOpen = false;
        try
        {
            var preflight = await _updates.PreflightAsync();
            UpdateSafetyBar.Severity = preflight.Safe ? InfoBarSeverity.Success : InfoBarSeverity.Warning;
            UpdateSafetyBar.Title = preflight.Safe ? "Update window is safe" : "Update is blocked";
            UpdateSafetyBar.Message = preflight.Summary;
        }
        catch (Exception ex)
        {
            UpdateSafetyBar.Severity = InfoBarSeverity.Error;
            UpdateSafetyBar.Title = "Update safety check failed";
            UpdateSafetyBar.Message = ex.Message;
        }
        UpdateSafetyBar.IsOpen = true;
        RenderPerformance();
    }

    private async void StageSignedUpdate_Click(object sender, RoutedEventArgs e)
    {
        UpdateSafetyBar.IsOpen = false;
        StageSignedUpdateButton.IsEnabled = false;
        ApplyStagedUpdateButton.IsEnabled = false;
        try
        {
            var window = App.Services.GetRequiredService<MainWindow>();
            var picker = new FileOpenPicker
            {
                SuggestedStartLocation = PickerLocationId.Downloads,
            };
            picker.FileTypeFilter.Add(".json");
            WinRT.Interop.InitializeWithWindow.Initialize(
                picker,
                WinRT.Interop.WindowNative.GetWindowHandle(window));

            StorageFile? file = await picker.PickSingleFileAsync();
            if (file is null)
            {
                return;
            }

            var properties = await file.GetBasicPropertiesAsync();
            if (properties.Size == 0 || properties.Size > UpdateCoordinator.MaxManifestChars * 4L)
            {
                throw new InvalidDataException("Signed update manifest is empty or exceeds the bounded input size.");
            }

            StagedUpdateText.Text = "Verifying signed metadata and staging update…";
            var manifestJson = await FileIO.ReadTextAsync(file);
            _stagedUpdate = await _updates.StageAsync(manifestJson);
            ApplyStagedUpdateButton.IsEnabled = true;
            StagedUpdateText.Text =
                $"Verified and staged {_stagedUpdate.Manifest.Version} · {UpdateCoordinator.ChannelName(_stagedUpdate.Manifest.Channel)} · SHA-256 {_stagedUpdate.Manifest.Sha256[..12]}… · rollback snapshot ready.";
            UpdateSafetyBar.Severity = InfoBarSeverity.Success;
            UpdateSafetyBar.Title = "Update staged safely";
            UpdateSafetyBar.Message = "MegasMoves.exe passed signature, hash and Windows PE identity checks. Applying it still requires your explicit confirmation.";
        }
        catch (Exception ex)
        {
            _stagedUpdate = null;
            StagedUpdateText.Text = "No verified update staged.";
            UpdateSafetyBar.Severity = InfoBarSeverity.Error;
            UpdateSafetyBar.Title = "Update staging failed";
            UpdateSafetyBar.Message = ex.Message;
        }
        finally
        {
            StageSignedUpdateButton.IsEnabled = true;
            ApplyStagedUpdateButton.IsEnabled = _stagedUpdate is not null;
            UpdateSafetyBar.IsOpen = true;
        }
    }

    private async void ApplyStagedUpdate_Click(object sender, RoutedEventArgs e)
    {
        if (_stagedUpdate is null)
        {
            return;
        }

        var dialog = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = $"Apply MegasMoves {_stagedUpdate.Manifest.Version}?",
            Content = "MegasMoves will close, replace only the verified Windows executable, restart, and automatically restore the rollback copy if the new build cannot reach authoritative startup.",
            PrimaryButtonText = "Apply and restart",
            CloseButtonText = "Cancel",
            DefaultButton = ContentDialogButton.Close,
        };
        if (await dialog.ShowAsync() != ContentDialogResult.Primary)
        {
            return;
        }

        UpdateSafetyBar.IsOpen = false;
        ApplyStagedUpdateButton.IsEnabled = false;
        StageSignedUpdateButton.IsEnabled = false;
        try
        {
            await _apply.LaunchAsync(_stagedUpdate);
            UpdateSafetyBar.Severity = InfoBarSeverity.Informational;
            UpdateSafetyBar.Title = "Update handoff started";
            UpdateSafetyBar.Message = "Closing MegasMoves so the authenticated one-shot updater can apply the verified build.";
            UpdateSafetyBar.IsOpen = true;
            Application.Current.Exit();
        }
        catch (Exception ex)
        {
            UpdateSafetyBar.Severity = InfoBarSeverity.Error;
            UpdateSafetyBar.Title = "Update apply failed";
            UpdateSafetyBar.Message = ex.Message;
            UpdateSafetyBar.IsOpen = true;
            ApplyStagedUpdateButton.IsEnabled = true;
            StageSignedUpdateButton.IsEnabled = true;
        }
    }

    private void PerformanceRefresh_Click(object sender, RoutedEventArgs e) => RenderPerformance();

    private void RenderPerformance()
    {
        PerformanceBudgetText.Text = _performance.Snapshot().Render();
    }
}
