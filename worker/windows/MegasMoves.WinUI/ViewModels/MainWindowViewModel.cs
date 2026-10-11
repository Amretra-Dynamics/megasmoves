using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MegasMoves.Services;

namespace MegasMoves.ViewModels;

public sealed class ProjectCardViewModel : ObservableObject
{
    private readonly MegasCoreBridge _bridge;
    private string _lastAction = "Ready";

    public ProjectCardViewModel(
        MegasCoreBridge bridge,
        string name,
        string root,
        string state,
        string targetId,
        string targetKind,
        string targetLabel,
        string service,
        int targetCount,
        int serviceCount)
    {
        _bridge = bridge;
        Name = name;
        Root = root;
        State = state;
        TargetId = targetId;
        TargetKind = targetKind;
        TargetLabel = targetLabel;
        Service = service;
        TargetCount = targetCount;
        ServiceCount = serviceCount;
        OpenCommand = new AsyncRelayCommand(OpenAsync, () => !string.IsNullOrWhiteSpace(TargetId));
        BuildCommand = new AsyncRelayCommand(BuildAsync, () => !string.IsNullOrWhiteSpace(TargetId));
        CheckCommand = new AsyncRelayCommand(CheckAsync, () => !string.IsNullOrWhiteSpace(Name));
        StopCommand = new AsyncRelayCommand(StopAsync, () => !string.IsNullOrWhiteSpace(Service));
        LogsCommand = new AsyncRelayCommand(LogsAsync, () => !string.IsNullOrWhiteSpace(Service));
    }

    public string Name { get; }
    public string Root { get; }
    public string State { get; }
    public string TargetId { get; }
    public string TargetKind { get; }
    public string TargetLabel { get; }
    public string Service { get; }
    public int TargetCount { get; }
    public int ServiceCount { get; }
    public string TargetSummary => TargetCount == 0 ? "No runnable targets" : $"{TargetCount} runnable target{(TargetCount == 1 ? "" : "s")}";
    public string ServiceSummary => ServiceCount == 0 ? "No runtime service" : $"{ServiceCount} service{(ServiceCount == 1 ? "" : "s")}";
    public bool IsReady => string.Equals(State, "ready", StringComparison.OrdinalIgnoreCase);

    public string LastAction
    {
        get => _lastAction;
        private set => SetProperty(ref _lastAction, value);
    }

    public IAsyncRelayCommand OpenCommand { get; }
    public IAsyncRelayCommand BuildCommand { get; }
    public IAsyncRelayCommand CheckCommand { get; }
    public IAsyncRelayCommand StopCommand { get; }
    public IAsyncRelayCommand LogsCommand { get; }

    private async Task OpenAsync()
    {
        LastAction = "Opening…";
        await _bridge.RequestAsync("target.action", new
        {
            project = Name,
            target_id = TargetId,
            action = "open"
        });
        LastAction = "Opened";
    }

    private async Task BuildAsync()
    {
        LastAction = "Building…";
        if (string.Equals(TargetKind, "web", StringComparison.OrdinalIgnoreCase) && !string.IsNullOrWhiteSpace(Service))
        {
            await _bridge.RequestAsync("workspace.action", new
            {
                project = Name,
                service = Service,
                action = "start"
            });
        }
        else
        {
            await _bridge.RequestAsync("target.action", new
            {
                project = Name,
                target_id = TargetId,
                action = "build"
            });
        }
        LastAction = "Build complete";
    }

    private async Task CheckAsync()
    {
        LastAction = "Checking…";
        await _bridge.RequestAsync("workspace.action", new
        {
            project = Name,
            service = string.IsNullOrWhiteSpace(Service) ? null : Service,
            action = "check"
        });
        LastAction = "Preflight complete";
    }

    private async Task StopAsync()
    {
        LastAction = "Stopping…";
        await _bridge.RequestAsync("workspace.action", new
        {
            project = Name,
            service = Service,
            action = "stop"
        });
        LastAction = "Stopped";
    }

    private async Task LogsAsync()
    {
        LastAction = "Reading logs…";
        await _bridge.RequestAsync("workspace.action", new
        {
            project = Name,
            service = Service,
            action = "logs",
            lines = 120
        });
        LastAction = "Logs captured";
    }
}

public sealed record ActivityItemViewModel(
    string Time,
    string Source,
    string State,
    string Operation,
    string Message,
    string Duration);

public sealed record DeviceItemViewModel(
    string Serial,
    string State,
    string Model,
    string Connection,
    bool Ready);

public sealed record AgentItemViewModel(
    string Name,
    string Detection,
    string Connection,
    string Configuration);

public sealed class RuntimeServiceViewModel
{
    public string Project { get; set; } = string.Empty;
    public string Service { get; set; } = string.Empty;
    public string State { get; set; } = string.Empty;
    public string Pid { get; set; } = string.Empty;
    public string Ports { get; set; } = string.Empty;
    public string OpenUrl { get; set; } = string.Empty;
    public string HealthUrl { get; set; } = string.Empty;
    public bool Ready { get; set; }
    public bool Running { get; set; }

    public RuntimeServiceViewModel() { }

    public RuntimeServiceViewModel(
        string project,
        string service,
        string state,
        string pid,
        string ports,
        string openUrl,
        string healthUrl,
        bool ready,
        bool running)
    {
        Project = project;
        Service = service;
        State = state;
        Pid = pid;
        Ports = ports;
        OpenUrl = openUrl;
        HealthUrl = healthUrl;
        Ready = ready;
        Running = running;
    }
}

public sealed record AttentionItemViewModel(
    string Title,
    string Detail,
    string Source,
    string Severity);

public sealed class TerminalSessionViewModel : ObservableObject
{
    private readonly MegasCoreBridge _bridge;
    private string _status;
    private int _exitCode;
    private string _output = string.Empty;

    public TerminalSessionViewModel(
        MegasCoreBridge bridge,
        string sessionId,
        string title,
        string command,
        string status,
        string cwd,
        int exitCode,
        long startedAt,
        long finishedAt)
    {
        _bridge = bridge;
        SessionId = sessionId;
        Title = string.IsNullOrWhiteSpace(title) ? sessionId : title;
        Command = command;
        _status = status;
        Cwd = cwd;
        _exitCode = exitCode;
        StartedAt = startedAt;
        FinishedAt = finishedAt;

        TerminateCommand = new AsyncRelayCommand(TerminateAsync);
        RefreshOutputCommand = new AsyncRelayCommand(RefreshOutputAsync);
    }

    public string SessionId { get; }
    public string Title { get; }
    public string Command { get; }
    public string Cwd { get; }
    public long StartedAt { get; }
    public long FinishedAt { get; }

    public string Status
    {
        get => _status;
        private set
        {
            if (SetProperty(ref _status, value))
            {
                OnPropertyChanged(nameof(IsRunning));
                OnPropertyChanged(nameof(StatusDisplay));
            }
        }
    }

    public int ExitCode
    {
        get => _exitCode;
        private set => SetProperty(ref _exitCode, value);
    }

    public string Output
    {
        get => _output;
        private set => SetProperty(ref _output, value);
    }

    public bool IsRunning => string.Equals(Status, "running", StringComparison.OrdinalIgnoreCase);
    public string StatusDisplay => IsRunning ? "Running" : $"Exited ({ExitCode})";

    public IAsyncRelayCommand TerminateCommand { get; }
    public IAsyncRelayCommand RefreshOutputCommand { get; }

    public async Task TerminateAsync()
    {
        await _bridge.RequestAsync("terminal.terminate", new { session_id = SessionId });
        Status = "terminated";
    }

    public async Task SendInputAsync(string text)
    {
        await _bridge.RequestAsync("terminal.input", new { session_id = SessionId, text });
    }

    public async Task RefreshOutputAsync()
    {
        var res = await _bridge.RequestAsync("terminal.read", new { session_id = SessionId, after_seq = -1, limit = 500 });
        if (res.TryGetProperty("events", out var events) && events.ValueKind == JsonValueKind.Array)
        {
            var sb = new System.Text.StringBuilder();
            foreach (var ev in events.EnumerateArray())
            {
                if (ev.TryGetProperty("text", out var t))
                {
                    sb.Append(t.GetString());
                }
            }
            Output = sb.ToString();
        }
    }
}

public partial class MainWindowViewModel : ObservableObject
{
    private readonly MegasCoreBridge _bridge;

    [ObservableProperty] private bool isBusy;
    [ObservableProperty] private string statusText = "Starting local runtime…";
    [ObservableProperty] private string errorMessage = string.Empty;
    [ObservableProperty] private string accountDisplay = "Local machine";
    [ObservableProperty] private string accountDetail = "Not signed in";
    [ObservableProperty] private bool accountAuthenticated;

    [ObservableProperty] private int trustedProjectCount;
    [ObservableProperty] private int runningProjectCount;
    [ObservableProperty] private int readyDeviceCount;
    [ObservableProperty] private string adbState = "Checking ADB…";
    [ObservableProperty] private string machineState = "Local core";
    [ObservableProperty] private int connectedAgentCount;
    [ObservableProperty] private int failureCount;
    [ObservableProperty] private int needsYouCount;
    [ObservableProperty] private string currentTaskText = "No canonical active task observed";
    [ObservableProperty] private string mcpStripText = "MCP starting";
    [ObservableProperty] private string runtimeStripText = "Runtime checking";
    [ObservableProperty] private string dbStripText = "DB migration pending";
    [ObservableProperty] private string localLatencyText = "Latency —";

    private readonly Microsoft.UI.Dispatching.DispatcherQueue? _dispatcher =
        Microsoft.UI.Dispatching.DispatcherQueue.GetForCurrentThread();

    public MainWindowViewModel(MegasCoreBridge bridge)
    {
        _bridge = bridge;
        RefreshCommand = new AsyncRelayCommand(RefreshAsync);
        SignInCommand = new AsyncRelayCommand(SignInAsync);
        SignOutCommand = new AsyncRelayCommand(SignOutAsync);
        RefreshActivityCommand = new AsyncRelayCommand(RefreshActivityAsync);
        _bridge.DiagnosticMessage += (_, message) =>
        {
            TrayIconManager.LogTray($"MegasCore stderr: {message}");
            SetStatusText(message);
        };
    }

    private void SetStatusText(string message)
    {
        var dispatcher = _dispatcher ?? Microsoft.UI.Dispatching.DispatcherQueue.GetForCurrentThread();
        if (dispatcher is not null && !dispatcher.HasThreadAccess)
        {
            dispatcher.TryEnqueue(() => StatusText = message);
        }
        else
        {
            StatusText = message;
        }
    }

    public ObservableCollection<ProjectCardViewModel> Projects { get; } = [];
    public ObservableCollection<ActivityItemViewModel> Activity { get; } = [];
    public ObservableCollection<ActivityItemViewModel> TestEvidence { get; } = [];
    public ObservableCollection<ActivityItemViewModel> LatestFailures { get; } = [];
    public ObservableCollection<DeviceItemViewModel> Devices { get; } = [];
    public ObservableCollection<AgentItemViewModel> Agents { get; } = [];
    public ObservableCollection<RuntimeServiceViewModel> RuntimeServices { get; } = [];
    public ObservableCollection<AttentionItemViewModel> NeedsYou { get; } = [];
    public ObservableCollection<TerminalSessionViewModel> Terminals { get; } = [];

    public IAsyncRelayCommand RefreshCommand { get; }
    public IAsyncRelayCommand SignInCommand { get; }
    public IAsyncRelayCommand SignOutCommand { get; }
    public IAsyncRelayCommand RefreshActivityCommand { get; }

    public bool HasError => !string.IsNullOrWhiteSpace(ErrorMessage);
    public bool HasProjects => Projects.Count > 0;
    public bool HasAttention => NeedsYou.Count > 0;

    public async Task InitializeAsync()
    {
        try
        {
            IsBusy = true;
            ErrorMessage = string.Empty;
            await _bridge.StartAsync();
            McpStripText = "MCP ready";
            await RefreshAsync();
        }
        catch (Exception ex)
        {
            TrayIconManager.LogTray($"MainWindowViewModel.InitializeAsync error: {ex}");
            ErrorMessage = ex.Message;
            OnPropertyChanged(nameof(HasError));
            McpStripText = "MCP unavailable";
            StatusText = "Local runtime unavailable";
        }
        finally
        {
            IsBusy = false;
        }
    }

    public async Task CreateTerminalAsync(string command)
    {
        if (string.IsNullOrWhiteSpace(command)) return;
        await RunAsync("Spawning terminal session…", async () =>
        {
            await _bridge.RequestAsync("terminal.create", new { command, title = command });
            await RefreshAsync();
        });
    }

    [RelayCommand]
    private async Task AddTrustedFolderAsync(string folder)
    {
        if (string.IsNullOrWhiteSpace(folder))
        {
            return;
        }
        await RunAsync("Adding trusted product…", async () =>
        {
            await _bridge.RequestAsync("workspace.add_folder", new { folder });
            await RefreshAsync();
        });
    }

    private async Task RefreshAsync()
    {
        await RunAsync("Refreshing operator state…", async () =>
        {
            var timer = Stopwatch.StartNew();
            var snapshot = await _bridge.RequestAsync("system.snapshot", new { });
            timer.Stop();
            LocalLatencyText = $"{Math.Max(1, timer.ElapsedMilliseconds)} ms local";
            ApplySnapshot(snapshot);
            McpStripText = "MCP ready";
            StatusText = "Operator state current";
        }, manageBusy: true);
    }

    private async Task RefreshActivityAsync()
    {
        await RunAsync("Refreshing Activity…", async () =>
        {
            var rows = await _bridge.RequestAsync("activity.list", new { limit = 300 });
            ApplyActivity(rows);
            StatusText = $"{Activity.Count} structured events";
        });
    }

    private async Task SignInAsync()
    {
        await RunAsync("Opening Amretra sign-in…", async () =>
        {
            await _bridge.RequestAsync("account.login", new { });
            await RefreshAsync();
        }, manageBusy: true);
    }

    private async Task SignOutAsync()
    {
        await RunAsync("Signing out…", async () =>
        {
            await _bridge.RequestAsync("account.logout", new { });
            await RefreshAsync();
        }, manageBusy: true);
    }

    private async Task RunAsync(string status, Func<Task> action, bool manageBusy = false)
    {
        try
        {
            if (manageBusy)
            {
                IsBusy = true;
            }
            ErrorMessage = string.Empty;
            OnPropertyChanged(nameof(HasError));
            StatusText = status;
            await action();
        }
        catch (Exception ex)
        {
            ErrorMessage = ex.Message;
            OnPropertyChanged(nameof(HasError));
            StatusText = "Action failed";
        }
        finally
        {
            if (manageBusy)
            {
                IsBusy = false;
            }
        }
    }

    private void ApplySnapshot(JsonElement snapshot)
    {
        Projects.Clear();
        Devices.Clear();
        Agents.Clear();
        RuntimeServices.Clear();
        NeedsYou.Clear();
        Terminals.Clear();

        if (snapshot.TryGetProperty("local_testing", out var localTesting))
        {
            if (localTesting.TryGetProperty("workspace", out var workspace))
            {
                TrustedProjectCount = Int(workspace, "project_count");
                RunningProjectCount = Int(workspace, "running_count");
                RuntimeStripText = RunningProjectCount > 0
                    ? $"Runtime {RunningProjectCount} running"
                    : "Runtime idle";

                if (workspace.TryGetProperty("projects", out var projects) && projects.ValueKind == JsonValueKind.Array)
                {
                    foreach (var project in projects.EnumerateArray())
                    {
                        var name = Text(project, "project");
                        var root = Text(project, "root");
                        var state = Text(project, "state");
                        var targetId = string.Empty;
                        var targetKind = string.Empty;
                        var targetLabel = string.Empty;
                        var service = string.Empty;
                        var targetCount = 0;
                        var serviceCount = 0;

                        if (string.Equals(state, "error", StringComparison.OrdinalIgnoreCase))
                        {
                            NeedsYou.Add(new AttentionItemViewModel(
                                name,
                                "Trusted project state could not be resolved. Open Runtime for evidence.",
                                "Runtime",
                                "error"));
                        }

                        if (project.TryGetProperty("targets", out var targets) && targets.ValueKind == JsonValueKind.Array)
                        {
                            targetCount = targets.GetArrayLength();
                            if (targetCount > 0)
                            {
                                var target = targets[0];
                                targetId = Text(target, "id");
                                targetKind = Text(target, "kind");
                                targetLabel = Text(target, "label");
                                service = Text(target, "service");
                            }
                        }

                        if (project.TryGetProperty("services", out var services) && services.ValueKind == JsonValueKind.Array)
                        {
                            serviceCount = services.GetArrayLength();
                            for (var index = 0; index < serviceCount; index++)
                            {
                                var row = services[index];
                                var serviceName = Text(row, "service");
                                if (string.IsNullOrWhiteSpace(service))
                                {
                                    service = serviceName;
                                }
                                var ready = Bool(row, "ready");
                                var running = Bool(row, "process_alive");
                                var serviceState = Text(row, "state");
                                if (string.IsNullOrWhiteSpace(serviceState))
                                {
                                    serviceState = ready ? "ready" : running ? "running" : "stopped";
                                }
                                RuntimeServices.Add(new RuntimeServiceViewModel(
                                    name,
                                    serviceName,
                                    serviceState,
                                    Text(row, "pid"),
                                    RenderPorts(row),
                                    Text(row, "open_url"),
                                    Text(row, "health_url"),
                                    ready,
                                    running));
                            }
                        }

                        Projects.Add(new ProjectCardViewModel(
                            _bridge,
                            name,
                            root,
                            state,
                            targetId,
                            targetKind,
                            targetLabel,
                            service,
                            targetCount,
                            serviceCount));
                    }
                }
            }

            if (localTesting.TryGetProperty("adb", out var adb))
            {
                ReadyDeviceCount = Int(adb, "ready_count");
                AdbState = Text(adb, "status");
                if (string.Equals(AdbState, "device-unauthorized", StringComparison.OrdinalIgnoreCase))
                {
                    NeedsYou.Add(new AttentionItemViewModel(
                        "Android authorization required",
                        "Approve this computer in the phone's USB debugging prompt.",
                        "Device",
                        "warning"));
                }
                else if (string.Equals(AdbState, "device-offline", StringComparison.OrdinalIgnoreCase))
                {
                    NeedsYou.Add(new AttentionItemViewModel(
                        "Android device offline",
                        "The attached device is visible to ADB but cannot accept a test run.",
                        "Device",
                        "warning"));
                }

                if (adb.TryGetProperty("devices", out var devices) && devices.ValueKind == JsonValueKind.Array)
                {
                    foreach (var device in devices.EnumerateArray())
                    {
                        Devices.Add(new DeviceItemViewModel(
                            Text(device, "serial"),
                            Text(device, "state"),
                            Text(device, "model"),
                            Text(device, "transport"),
                            Bool(device, "ready")));
                    }
                }
            }
        }

        if (snapshot.TryGetProperty("terminals", out var terminals) && terminals.ValueKind == JsonValueKind.Array)
        {
            foreach (var term in terminals.EnumerateArray())
            {
                var sessionId = Text(term, "session_id");
                var title = Text(term, "title");
                var status = Text(term, "status");
                var cwd = Text(term, "cwd");
                var exitCode = Int(term, "exit_code");
                var startedAt = Long(term, "started_at");
                var finishedAt = Long(term, "finished_at");
                var cmd = Text(term, "command");
                if (string.IsNullOrWhiteSpace(cmd) && term.TryGetProperty("command", out var cmdProp) && cmdProp.ValueKind == JsonValueKind.Array)
                {
                    cmd = string.Join(" ", cmdProp.EnumerateArray().Select(c => c.GetString() ?? ""));
                }

                Terminals.Add(new TerminalSessionViewModel(
                    _bridge,
                    sessionId,
                    title,
                    cmd,
                    status,
                    cwd,
                    exitCode,
                    startedAt,
                    finishedAt));
            }
        }

        if (snapshot.TryGetProperty("account", out var account))
        {
            AccountAuthenticated = Bool(account, "amretra_authenticated");
            if (account.TryGetProperty("user", out var user))
            {
                var fullName = Text(user, "full_name");
                var email = Text(user, "email");
                AccountDisplay = string.IsNullOrWhiteSpace(fullName)
                    ? (string.IsNullOrWhiteSpace(email) ? "Amretra account" : email)
                    : fullName;
                AccountDetail = email;
            }
            else
            {
                AccountDisplay = "Local machine";
                AccountDetail = "Account capabilities optional";
            }
        }

        if (snapshot.TryGetProperty("machine", out var machine))
        {
            var configured = Int(machine, "configured_servers");
            var running = Int(machine, "running_servers");
            MachineState = $"{running}/{configured} MCP runtimes running";
            CurrentTaskText = string.IsNullOrWhiteSpace(Text(machine, "active_session_id"))
                ? "No canonical active task observed"
                : $"Session {Text(machine, "active_session_id")}";
        }

        if (snapshot.TryGetProperty("agents", out var agents) && agents.ValueKind == JsonValueKind.Array)
        {
            foreach (var agent in agents.EnumerateArray())
            {
                var configured = NullableBool(agent, "megas_configured");
                Agents.Add(new AgentItemViewModel(
                    Text(agent, "client"),
                    Bool(agent, "detected") ? "Detected" : "Not detected",
                    configured is null ? "Unknown" : configured.Value ? "Connected" : "Not connected",
                    Text(agent, "config_path")));
            }
        }
        ConnectedAgentCount = Agents.Count(row => string.Equals(row.Connection, "Connected", StringComparison.Ordinal));

        if (snapshot.TryGetProperty("activity", out var activity))
        {
            ApplyActivity(activity);
        }

        NeedsYouCount = NeedsYou.Count;
        OnPropertyChanged(nameof(HasProjects));
        OnPropertyChanged(nameof(HasAttention));
    }

    private void ApplyActivity(JsonElement rows)
    {
        Activity.Clear();
        TestEvidence.Clear();
        LatestFailures.Clear();

        if (rows.ValueKind != JsonValueKind.Array)
        {
            FailureCount = 0;
            return;
        }

        foreach (var row in rows.EnumerateArray())
        {
            var item = new ActivityItemViewModel(
                Text(row, "at"),
                Text(row, "source"),
                Text(row, "severity"),
                Text(row, "operation_id"),
                Text(row, "message"),
                row.TryGetProperty("duration_ms", out var duration) && duration.ValueKind == JsonValueKind.Number
                    ? $"{duration.GetDouble():0} ms"
                    : string.Empty);
            Activity.Add(item);

            if (string.Equals(item.Source, "Test", StringComparison.OrdinalIgnoreCase))
            {
                TestEvidence.Add(item);
            }
            if (string.Equals(item.State, "error", StringComparison.OrdinalIgnoreCase))
            {
                LatestFailures.Add(item);
            }
        }

        while (LatestFailures.Count > 6)
        {
            LatestFailures.RemoveAt(0);
        }
        while (TestEvidence.Count > 40)
        {
            TestEvidence.RemoveAt(0);
        }

        FailureCount = LatestFailures.Count;
        foreach (var failure in LatestFailures.TakeLast(3))
        {
            NeedsYou.Add(new AttentionItemViewModel(
                failure.Message,
                string.IsNullOrWhiteSpace(failure.Operation) ? "Failure evidence is available in Activity." : $"Operation {failure.Operation}",
                failure.Source,
                "error"));
        }
        NeedsYouCount = NeedsYou.Count;
        OnPropertyChanged(nameof(HasAttention));
    }

    private static string RenderPorts(JsonElement row)
    {
        if (!row.TryGetProperty("ports", out var ports))
        {
            return string.Empty;
        }
        if (ports.ValueKind == JsonValueKind.Array)
        {
            return string.Join(", ", ports.EnumerateArray().Select(value => value.ToString()));
        }
        return ports.ToString();
    }

    private static string Text(JsonElement value, string property)
    {
        return value.TryGetProperty(property, out var item) && item.ValueKind != JsonValueKind.Null
            ? item.ToString()
            : string.Empty;
    }

    private static long Long(JsonElement value, string property)
    {
        return value.TryGetProperty(property, out var item) && item.TryGetInt64(out var result) ? result : 0;
    }

    private static int Int(JsonElement value, string property)
    {
        return value.TryGetProperty(property, out var item) && item.TryGetInt32(out var result) ? result : 0;
    }

    private static bool Bool(JsonElement value, string property)
    {
        return value.TryGetProperty(property, out var item) && item.ValueKind is JsonValueKind.True;
    }

    private static bool? NullableBool(JsonElement value, string property)
    {
        if (!value.TryGetProperty(property, out var item) || item.ValueKind == JsonValueKind.Null)
        {
            return null;
        }
        return item.ValueKind == JsonValueKind.True;
    }
}
