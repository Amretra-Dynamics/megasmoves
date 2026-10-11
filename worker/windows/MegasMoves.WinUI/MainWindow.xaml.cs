using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Composition.SystemBackdrops;
using MegasMoves.Pages;
using MegasMoves.Services;
using MegasMoves.ViewModels;
using Windows.Graphics;

namespace MegasMoves;

public sealed partial class MainWindow : Window
{
    private const int DefaultWidth = 1440;
    private const int DefaultHeight = 900;
    private const int MinWidth = 1040;
    private const int MinHeight = 680;
    private const int MaxRememberedWidth = 7680;
    private const int MaxRememberedHeight = 4320;

    private static readonly CommandTarget[] CommandTargets =
    {
        new("Overview", "overview", "machine health needs-you failures summary"),
        new("Operations", "operations", "trusted apps services ports start build check local testing"),
        new("Agents", "agents", "sessions execution evidence clients tasks"),
        new("Devices", "devices", "adb android emulators targets connected devices"),
        new("Runtime", "runtime", "trusted apps processes services ports local testing"),
        new("Tests", "tests", "preflight normalized test evidence"),
        new("Activity", "activity", "events operations correlations failures logs timeline"),
        new("Account", "account", "account identity and permissions"),
        new("Advanced", "advanced", "raw logs connections ports devices diagnostics"),
        new("Settings", "settings", "windows updates export performance theme"),
    };

    private readonly UiStateStore _uiState;
    private readonly PerformanceBudgetMonitor _performance;
    private readonly ThemeService _themeService;
    private readonly AppWindow _appWindow;
    private TrayIconManager? _trayIcon;

    public MainWindowViewModel ViewModel { get; }

    public MainWindow(
        MainWindowViewModel viewModel,
        UiStateStore uiState,
        PerformanceBudgetMonitor performance,
        ThemeService themeService)
    {
        ViewModel = viewModel;
        _uiState = uiState;
        _performance = performance;
        _themeService = themeService;
        InitializeComponent();

        Title = "MegasMoves";
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        SystemBackdrop = new MicaBackdrop { Kind = MicaKind.BaseAlt };
        SetApplicationIcon();
        ApplyTheme(_themeService.CurrentMode);
        _themeService.ThemeChanged += ApplyTheme;

        CommandCenter.ItemsSource = CommandTargets.Select(item => item.Name).ToArray();
        RestoreWindowGeometry();
        RestorePaneState();

        _appWindow = CurrentAppWindow();
        _appWindow.Closing += OnAppWindowClosing;
        Activated += OnActivated;
        Closed += OnClosed;

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        _trayIcon = new TrayIconManager(hwnd, RestoreWindow, ForceClose, HandleCloseToTray);
    }

    private void SetApplicationIcon()
    {
        try
        {
            var iconPath = RuntimePayloadResolver.ResolveAsset("app.ico")
                ?? Path.Combine(AppContext.BaseDirectory, "Assets", "app.ico");
            if (File.Exists(iconPath))
            {
                CurrentAppWindow().SetIcon(iconPath);
            }
        }
        catch
        {
            // Best-effort presentation icon only.
        }
    }

    [System.Runtime.InteropServices.DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);
    private const int DwmwaUseImmersiveDarkMode = 20;

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool IsWindow(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Auto)]
    private static extern int GetClassName(IntPtr hWnd, System.Text.StringBuilder lpClassName, int nMaxCount);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern IntPtr GetParent(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetThreadDesktop(uint dwThreadId);

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private static extern bool GetUserObjectInformationW(IntPtr hObj, int nIndex, [System.Runtime.InteropServices.Out] byte[] pvInfo, int nLength, out int lpnLengthNeeded);

    private struct RECT { public int Left, Top, Right, Bottom; }
    private const int SwRestore = 9;

    public static bool IsSystemDarkTheme()
    {
        try
        {
            using var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
            if (key?.GetValue("AppsUseLightTheme") is int val)
            {
                return val == 0;
            }
        }
        catch { }
        return true;
    }

    public void ApplyTheme(AppThemeMode mode)
    {
        try
        {
            var isDark = mode switch
            {
                AppThemeMode.Dark => true,
                AppThemeMode.Light => false,
                _ => IsSystemDarkTheme(),
            };

            var elementTheme = isDark ? ElementTheme.Dark : ElementTheme.Light;

            if (Content is FrameworkElement root)
            {
                root.RequestedTheme = elementTheme;
            }

            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            int useDarkMode = isDark ? 1 : 0;
            DwmSetWindowAttribute(hwnd, DwmwaUseImmersiveDarkMode, ref useDarkMode, sizeof(int));

            var titleBar = CurrentAppWindow().TitleBar;
            if (titleBar is not null)
            {
                titleBar.ButtonBackgroundColor = Microsoft.UI.Colors.Transparent;
                titleBar.ButtonInactiveBackgroundColor = Microsoft.UI.Colors.Transparent;
                if (isDark)
                {
                    titleBar.ButtonForegroundColor = Windows.UI.Color.FromArgb(255, 240, 240, 240);
                    titleBar.ButtonHoverForegroundColor = Windows.UI.Color.FromArgb(255, 255, 255, 255);
                    titleBar.ButtonHoverBackgroundColor = Windows.UI.Color.FromArgb(30, 255, 255, 255);
                }
                else
                {
                    titleBar.ButtonForegroundColor = Windows.UI.Color.FromArgb(255, 30, 30, 30);
                    titleBar.ButtonHoverForegroundColor = Windows.UI.Color.FromArgb(255, 0, 0, 0);
                    titleBar.ButtonHoverBackgroundColor = Windows.UI.Color.FromArgb(20, 0, 0, 0);
                }
            }

            if (ThemeIcon is not null)
            {
                ThemeIcon.Glyph = isDark ? "\uE708" : "\uE706";
            }
        }
        catch
        {
            // Theme presentation update is best-effort.
        }
    }

    private void ThemeToggle_Click(object sender, RoutedEventArgs e)
    {
        _themeService.ToggleTheme();
    }

    private async void OnActivated(object sender, WindowActivatedEventArgs args)
    {
        TrayIconManager.LogTray($"MainWindow.OnActivated fired: ActivationState={args.WindowActivationState}");
        Activated -= OnActivated;
        var lastNav = _uiState.GetString("navigation.last") ?? "overview";
        TrayIconManager.LogTray($"MainWindow.OnActivated: selecting navigation '{lastNav}'");
        var restored = SelectNavigation(lastNav);
        if (!restored)
        {
            TrayIconManager.LogTray("MainWindow.OnActivated: navigating to OverviewPage");
            ContentFrame.Navigate(typeof(OverviewPage));
        }
        TrayIconManager.LogTray("MainWindow.OnActivated: initializing ViewModel");
        await ViewModel.InitializeAsync();
        TrayIconManager.LogTray($"MainWindow.OnActivated: ViewModel initialized (HasError={ViewModel.HasError}, ErrorMessage='{ViewModel.ErrorMessage}')");
        _performance.MarkWarmLaunch(App.LaunchClock.Elapsed);
        _trayIcon?.EnsureTrayIcon();

        // A post-update build and a release smoke are accepted only after the
        // same authenticated authoritative startup path used by Overview has
        // completed successfully.
        if (!ViewModel.HasError)
        {
            await UpdateApplyWorker.SignalReadyIfRequestedAsync();
            if (App.ReleaseSmokeFile is string smokeFile)
            {
                TrayIconManager.LogTray("MainWindow.OnActivated: release smoke requested, exiting");
                await SignalReleaseSmokeReadyAsync(smokeFile);
                Application.Current.Exit();
            }
        }
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        SetWindowPos(hwnd, new IntPtr(-1) /* HWND_TOPMOST */, 0, 0, 0, 0, 0x0003 /* SWP_NOSIZE | SWP_NOMOVE */ | 0x0040 /* SWP_SHOWWINDOW */);
        SetWindowPos(hwnd, new IntPtr(-2) /* HWND_NOTOPMOST */, 0, 0, 0, 0, 0x0003 | 0x0040);
        SetForegroundWindow(hwnd);
        TrayIconManager.LogTray("MainWindow.OnActivated completed and brought to foreground");
    }

    private const string ReleaseSmokeFormat = "megasmoves-release-smoke";

    private static async Task SignalReleaseSmokeReadyAsync(string path)
    {
        var directory = Path.GetDirectoryName(path)
            ?? throw new InvalidOperationException("release smoke path has no parent directory");
        Directory.CreateDirectory(directory);
        var temporary = path + ".tmp";
        var payload = $"{{\"format\":\"{ReleaseSmokeFormat}\",\"version\":1,\"ready\":true}}\n";
        await File.WriteAllTextAsync(temporary, payload);
        File.Move(temporary, path, overwrite: true);
    }

    private void AccountButton_Click(object sender, RoutedEventArgs e)
    {
        NavigateTo("account");
    }

    private void McpStatus_Click(object sender, RoutedEventArgs e)
    {
        NavigateTo("activity");
    }

    private void RuntimeStatus_Click(object sender, RoutedEventArgs e)
    {
        NavigateTo("runtime");
    }

    private void DatabaseStatus_Click(object sender, RoutedEventArgs e)
    {
        NavigateTo("advanced");
    }

    private void LatencyStatus_Click(object sender, RoutedEventArgs e)
    {
        NavigateTo("activity");
    }

    private void CommandCenter_AcceleratorInvoked(KeyboardAccelerator sender, KeyboardAcceleratorInvokedEventArgs args)
    {
        CommandCenter.ItemsSource = CommandTargets.Select(item => item.Name).ToArray();
        CommandCenter.IsSuggestionListOpen = true;
        CommandCenter.Focus(FocusState.Programmatic);
        args.Handled = true;
    }

    private void CommandCenter_TextChanged(AutoSuggestBox sender, AutoSuggestBoxTextChangedEventArgs args)
    {
        if (args.Reason != AutoSuggestionBoxTextChangeReason.UserInput)
        {
            return;
        }

        var needle = sender.Text.Trim();
        var matches = CommandTargets
            .Where(item =>
                string.IsNullOrWhiteSpace(needle) ||
                item.Name.Contains(needle, StringComparison.OrdinalIgnoreCase) ||
                item.Keywords.Contains(needle, StringComparison.OrdinalIgnoreCase))
            .Select(item => item.Name)
            .ToArray();
        sender.ItemsSource = matches;
        sender.IsSuggestionListOpen = matches.Length > 0;
    }

    private void CommandCenter_SuggestionChosen(AutoSuggestBox sender, AutoSuggestBoxSuggestionChosenEventArgs args)
    {
        if (args.SelectedItem is string selected)
        {
            sender.Text = selected;
        }
    }

    private void CommandCenter_QuerySubmitted(AutoSuggestBox sender, AutoSuggestBoxQuerySubmittedEventArgs args)
    {
        var text = (args.ChosenSuggestion as string ?? sender.Text).Trim();
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }

        var target = CommandTargets.FirstOrDefault(item =>
            string.Equals(item.Name, text, StringComparison.OrdinalIgnoreCase));
        target ??= CommandTargets.FirstOrDefault(item =>
            item.Name.Contains(text, StringComparison.OrdinalIgnoreCase) ||
            item.Keywords.Contains(text, StringComparison.OrdinalIgnoreCase));
        if (target is null)
        {
            return;
        }

        NavigateTo(target.Tag);
        sender.Text = string.Empty;
        sender.IsSuggestionListOpen = false;
    }

    private void NavigateTo(string tag)
    {
        if (!SelectNavigation(tag))
        {
            ContentFrame.Navigate(PageForTag(tag));
        }
    }

    private void Navigation_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItemContainer?.Tag is not string tag)
        {
            return;
        }

        _uiState.SetString("navigation.last", tag);
        var page = PageForTag(tag);
        if (ContentFrame.CurrentSourcePageType != page)
        {
            ContentFrame.Navigate(page);
        }
    }

    private void RestorePaneState()
    {
        try
        {
            var paneState = _uiState.GetString("navigation.pane_open");
            Navigation.IsPaneOpen = paneState is null || string.Equals(paneState, "true", StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            Navigation.IsPaneOpen = true;
        }
    }

    private void Navigation_PaneOpened(NavigationView sender, object args)
    {
        _uiState.SetString("navigation.pane_open", "true");
    }

    private void Navigation_PaneClosed(NavigationView sender, object args)
    {
        _uiState.SetString("navigation.pane_open", "false");
    }

    private static Type PageForTag(string tag) => tag switch
    {
        "overview" => typeof(OverviewPage),
        "operations" => typeof(LocalTestingPage),
        "agents" => typeof(AgentsPage),
        "devices" => typeof(DevicesPage),
        "runtime" => typeof(RuntimePage),
        "tests" => typeof(TestsPage),
        "activity" => typeof(ActivityPage),
        "account" => typeof(AccountPage),
        "advanced" => typeof(AdvancedPage),
        "settings" => typeof(SettingsPage),
        _ => typeof(OverviewPage),
    };

    private bool SelectNavigation(string tag)
    {
        foreach (var item in Navigation.MenuItems.OfType<NavigationViewItem>()
                     .Concat(Navigation.FooterMenuItems.OfType<NavigationViewItem>()))
        {
            if (!string.Equals(item.Tag?.ToString(), tag, StringComparison.Ordinal))
            {
                continue;
            }

            Navigation.SelectedItem = item;
            if (ContentFrame.CurrentSourcePageType != PageForTag(tag))
            {
                ContentFrame.Navigate(PageForTag(tag));
            }
            return true;
        }
        return false;
    }

    private void RestoreWindowGeometry()
    {
        try
        {
            var appWindow = CurrentAppWindow();
            var width = ClampRememberedDimension(
                _uiState.GetInt32("window.width"),
                DefaultWidth,
                MinWidth,
                MaxRememberedWidth);
            var height = ClampRememberedDimension(
                _uiState.GetInt32("window.height"),
                DefaultHeight,
                MinHeight,
                MaxRememberedHeight);
            var displayArea = DisplayArea.GetFromWindowId(appWindow.Id, DisplayAreaFallback.Primary);
            if (displayArea != null)
            {
                int x = Math.Max(20, (displayArea.WorkArea.Width - width) / 2);
                int y = Math.Max(20, (displayArea.WorkArea.Height - height) / 2);
                TrayIconManager.LogTray($"RestoreWindowGeometry: moving to ({x}, {y}) and resizing to {width}x{height}");
                appWindow.MoveAndResize(new RectInt32(x, y, width, height));
            }
            else
            {
                TrayIconManager.LogTray($"RestoreWindowGeometry: resizing to {width}x{height}");
                appWindow.Resize(new SizeInt32(width, height));
            }
        }
        catch (Exception ex)
        {
            TrayIconManager.LogTray($"RestoreWindowGeometry exception: {ex.Message}");
            // UI-local persistence/sizing is non-authoritative. Failure must not
            // prevent the local trust/execution operator from opening.
        }
    }

    private bool _allowClose;

    public void RestoreWindow()
    {
        DispatcherQueue.TryEnqueue(() =>
        {
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            uint tid = GetCurrentThreadId();
            IntPtr hDesk = GetThreadDesktop(tid);
            byte[] deskNameBuf = new byte[256];
            int needed;
            GetUserObjectInformationW(hDesk, 2 /* UOI_NAME */, deskNameBuf, deskNameBuf.Length, out needed);
            string deskName = needed > 2 ? System.Text.Encoding.Unicode.GetString(deskNameBuf, 0, needed - 2) : "unknown";

            bool isWin = IsWindow(hwnd);
            bool isVis = IsWindowVisible(hwnd);
            RECT r;
            GetWindowRect(hwnd, out r);
            var cls = new System.Text.StringBuilder(256);
            GetClassName(hwnd, cls, 256);
            IntPtr parent = GetParent(hwnd);

            TrayIconManager.LogTray($"RestoreWindow: hwnd=0x{hwnd.ToInt64():X}, TID={tid}, Desktop='{deskName}', IsWin={isWin}, IsVis={isVis}, Rect=({r.Left},{r.Top})-({r.Right},{r.Bottom}), Class='{cls}', Parent=0x{parent.ToInt64():X}");

            var showRes = ShowWindow(hwnd, SwRestore);
            _appWindow?.Show();
            Activate();

            SetWindowPos(hwnd, new IntPtr(-1) /* HWND_TOPMOST */, 0, 0, 0, 0, 0x0003 /* SWP_NOSIZE | SWP_NOMOVE */ | 0x0040 /* SWP_SHOWWINDOW */);
            SetWindowPos(hwnd, new IntPtr(-2) /* HWND_NOTOPMOST */, 0, 0, 0, 0, 0x0003 | 0x0040);
            SetForegroundWindow(hwnd);
            TrayIconManager.LogTray("RestoreWindow: SetWindowPos TOPMOST and SetForegroundWindow called");
        });
    }

    public bool HandleCloseToTray()
    {
        if (_allowClose)
        {
            return false;
        }

        var behavior = _uiState.GetString("window.close_behavior") ?? "dock";
        TrayIconManager.LogTray($"HandleCloseToTray: behavior={behavior}");
        if (!string.Equals(behavior, "exit", StringComparison.OrdinalIgnoreCase))
        {
            DispatcherQueue.TryEnqueue(() =>
            {
                var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
                ShowWindow(hwnd, 0); // SW_HIDE
                _appWindow?.Hide();
                TrayIconManager.LogTray("MainWindow hidden to system tray");
                _trayIcon?.EnsureTrayIcon();

                var hintShown = _uiState.GetBool("window.dock_hint_shown") ?? false;
                if (!hintShown)
                {
                    _uiState.SetBool("window.dock_hint_shown", true);
                    _trayIcon?.ShowBalloonNotification(
                        "MegasMoves is still running",
                        "MegasMoves minimized to your system tray to keep local testing runtimes and Megas MCP connections running. Click this icon to open it.");
                }
            });
            return true;
        }

        return false;
    }

    private void OnAppWindowClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        TrayIconManager.LogTray($"OnAppWindowClosing fired: allowClose={_allowClose}");
        if (_allowClose)
        {
            return;
        }

        if (HandleCloseToTray())
        {
            args.Cancel = true;
            TrayIconManager.LogTray("OnAppWindowClosing: args.Cancel set to true");
        }
        else
        {
            ForceClose();
        }
    }

    public void ForceClose()
    {
        DispatcherQueue.TryEnqueue(() =>
        {
            TrayIconManager.LogTray("ForceClose called, exiting application");
            _allowClose = true;
            _trayIcon?.Dispose();
            Close();
            Application.Current.Exit();
        });
    }

    private void OnClosed(object sender, WindowEventArgs args)
    {
        TrayIconManager.LogTray("MainWindow.OnClosed fired");
        _trayIcon?.Dispose();
        _themeService.ThemeChanged -= ApplyTheme;
        try
        {
            var size = CurrentAppWindow().Size;
            _uiState.SetInt32("window.width", size.Width);
            _uiState.SetInt32("window.height", size.Height);
        }
        catch
        {
            // Best-effort presentation state only.
        }
    }

    private AppWindow CurrentAppWindow()
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Microsoft.UI.Win32Interop.GetWindowIdFromWindow(hwnd);
        return AppWindow.GetFromWindowId(windowId);
    }

    private static int ClampRememberedDimension(int? value, int fallback, int minimum, int maximum)
    {
        if (value is null)
        {
            return fallback;
        }
        return Math.Clamp(value.Value, minimum, maximum);
    }

    private sealed record CommandTarget(string Name, string Tag, string Keywords);
}
