using System.Diagnostics;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.UI.Xaml;
using MegasMoves.Native;
using MegasMoves.Services;
using MegasMoves.ViewModels;

namespace MegasMoves;

public partial class App : Application
{
    internal static Stopwatch LaunchClock { get; } = Stopwatch.StartNew();
    internal static string? ReleaseSmokeFile { get; private set; }

    private readonly IHost _host;
    private Window? _window;

    public App()
    {
        UnhandledException += (sender, e) =>
        {
            LogCrash("App.UnhandledException: " + e.Message + "\n" + e.Exception);
        };
        AppDomain.CurrentDomain.UnhandledException += (sender, e) =>
        {
            LogCrash("AppDomain.UnhandledException: " + e.ExceptionObject);
        };
        TaskScheduler.UnobservedTaskException += (sender, e) =>
        {
            LogCrash("TaskScheduler.UnobservedTaskException: " + e.Exception);
        };

        InitializeComponent();
        _host = Host.CreateDefaultBuilder()
            .ConfigureServices(services =>
            {
                services.AddSingleton<UiStateStore>();
                services.AddSingleton<ThemeService>();
                services.AddSingleton<PerformanceBudgetMonitor>();
                services.AddSingleton<MegasNativeRuntime>();
                services.AddSingleton<MegasCoreBridge>();
                services.AddSingleton<UpdateCoordinator>();
                services.AddSingleton<UpdateApplyLauncher>();
                services.AddHostedService<NativeRuntimeHostedService>();
                services.AddSingleton<MainWindowViewModel>();
                services.AddSingleton<LocalTestingViewModel>();
                services.AddSingleton<AgentsViewModel>();
                services.AddSingleton<ActivityViewModel>();
                services.AddSingleton<DiagnosticsViewModel>();
                services.AddSingleton<MainWindow>();
            })
            .Build();
    }

    public static IServiceProvider Services => ((App)Current)._host.Services;

    protected override async void OnLaunched(LaunchActivatedEventArgs args)
    {
        var commandLine = Environment.GetCommandLineArgs();
        var stdioWorkerResult = await CoreStdioWorker.RunIfRequestedAsync(commandLine);
        if (stdioWorkerResult is int stdioExitCode)
        {
            Environment.Exit(stdioExitCode);
            return;
        }

        var updateWorkerResult = await UpdateApplyWorker.RunIfRequestedAsync(commandLine);
        if (updateWorkerResult is int exitCode)
        {
            Environment.Exit(exitCode);
            return;
        }

        ReleaseSmokeFile = ResolveReleaseSmokeFile();
        TrayIconManager.LogTray($"App.OnLaunched: ReleaseSmokeFile='{ReleaseSmokeFile}'");
        TrayIconManager.LogTray("App.OnLaunched: starting host");
        await _host.StartAsync();
        TrayIconManager.LogTray("App.OnLaunched: host started, resolving MainWindow");
        _window = Services.GetRequiredService<MainWindow>();
        TrayIconManager.LogTray("App.OnLaunched: activating MainWindow");
        _window.Activate();
        TrayIconManager.LogTray("App.OnLaunched: MainWindow.Activate completed");
    }

    private static string? ResolveReleaseSmokeFile()
    {
        var raw = Environment.GetEnvironmentVariable("MEGAS_RELEASE_SMOKE_FILE")?.Trim();
        if (string.IsNullOrWhiteSpace(raw))
        {
            return null;
        }

        try
        {
            var root = Path.GetFullPath(Path.Combine(Path.GetTempPath(), "MegasMoves-release-smoke"));
            var candidate = Path.GetFullPath(raw);
            var rootPrefix = root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                + Path.DirectorySeparatorChar;
            if (!candidate.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase))
            {
                return null;
            }
            if (!string.Equals(Path.GetExtension(candidate), ".json", StringComparison.OrdinalIgnoreCase))
            {
                return null;
            }
            return candidate;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static void LogCrash(string message)
    {
        try
        {
            var logPath = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Amretra Dynamics",
                "MegasMoves",
                "crash.log");
            File.AppendAllText(logPath, $"[{DateTime.UtcNow:O}] {message}\n");
        }
        catch { }
    }
}
