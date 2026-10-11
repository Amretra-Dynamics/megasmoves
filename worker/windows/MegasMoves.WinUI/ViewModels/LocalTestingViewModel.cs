using System.Collections.ObjectModel;
using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MegasMoves.Services;

namespace MegasMoves.ViewModels;

public sealed record LocalTestingProjectItemViewModel(
    string Name,
    string Root,
    string State,
    int TargetCount,
    int ServiceCount,
    string Problem)
{
    public string TargetSummary => $"{TargetCount} target{(TargetCount == 1 ? "" : "s")}";
    public string ServiceSummary => $"{ServiceCount} service{(ServiceCount == 1 ? "" : "s")}";
    public string StateSummary => string.IsNullOrWhiteSpace(Problem) ? State : $"{State} · {Problem}";
}

public sealed record LocalTestingTargetItemViewModel(
    string Project,
    string Id,
    string Label,
    string Kind,
    string Runtime,
    string BuildLabel,
    string OpenLabel,
    string Evidence,
    string Service,
    bool Ready,
    bool Running)
{
    public string TypeLabel => string.IsNullOrWhiteSpace(Kind) ? "unknown" : Kind;
    public string RuntimeLabel => string.IsNullOrWhiteSpace(Runtime) ? "local" : Runtime;
    public string StateLabel => Kind.Equals("web", StringComparison.OrdinalIgnoreCase)
        ? Ready ? "Ready" : Running ? "Starting / degraded" : "Stopped"
        : "Detected";
    public bool RequiresAndroidDevice => Kind.Equals("android", StringComparison.OrdinalIgnoreCase);
    public bool CanBuild => !Kind.Equals("web", StringComparison.OrdinalIgnoreCase) || !string.IsNullOrWhiteSpace(Service);
    public bool CanOpen => !string.IsNullOrWhiteSpace(Id);
    public bool CanStopAndroid => RequiresAndroidDevice || Kind.Equals("linux", StringComparison.OrdinalIgnoreCase);
    public bool CanReadAndroidLogs => RequiresAndroidDevice;
}

public sealed record LocalTestingServiceItemViewModel(
    string Project,
    string Name,
    string State,
    string Pid,
    string Ports,
    string OpenUrl,
    string HealthUrl,
    bool Ready,
    bool Running)
{
    public string StateLabel => Ready ? "Ready" : Running ? "Starting / degraded" : string.IsNullOrWhiteSpace(State) ? "Stopped" : State;
    public bool CanStart => !Running;
    public bool CanStop => Running;
}

public partial class LocalTestingViewModel : ObservableObject
{
    private readonly MegasCoreBridge _bridge;

    [ObservableProperty] private bool isBusy;
    [ObservableProperty] private string statusText = "Loading local testing state…";
    [ObservableProperty] private string errorMessage = string.Empty;
    [ObservableProperty] private int trustedProjectCount;
    [ObservableProperty] private int runnableTargetCount;
    [ObservableProperty] private int runningServiceCount;
    [ObservableProperty] private int readyDeviceCount;
    [ObservableProperty] private string adbState = "Checking ADB…";

    [ObservableProperty] private DeviceItemViewModel? selectedDevice;

    public LocalTestingViewModel(MegasCoreBridge bridge)
    {
        _bridge = bridge;
    }

    public ObservableCollection<LocalTestingProjectItemViewModel> Projects { get; } = [];
    public ObservableCollection<LocalTestingTargetItemViewModel> Targets { get; } = [];
    public ObservableCollection<LocalTestingServiceItemViewModel> Services { get; } = [];
    public ObservableCollection<DeviceItemViewModel> Devices { get; } = [];

    public bool HasError => !string.IsNullOrWhiteSpace(ErrorMessage);
    public bool HasProjects => Projects.Count > 0;
    public bool HasTargets => Targets.Count > 0;
    public bool HasServices => Services.Count > 0;
    public bool HasDevices => Devices.Count > 0;

    public async Task InitializeAsync()
    {
        if (!IsBusy && Projects.Count == 0)
        {
            await RefreshAsync();
        }
    }

    [RelayCommand]
    private async Task RefreshAsync()
    {
        await RunAsync("Refreshing trusted projects, targets and runtime evidence…", async () =>
        {
            var snapshot = await _bridge.RequestAsync("system.snapshot", new { });
            ApplySnapshot(snapshot);
            StatusText = $"{TrustedProjectCount} trusted · {RunnableTargetCount} targets · {RunningServiceCount} running services";
        });
    }

    [RelayCommand]
    private async Task AddTrustedFolderAsync(string folder)
    {
        if (string.IsNullOrWhiteSpace(folder))
        {
            return;
        }
        await RunAsync("Adding trusted product folder…", async () =>
        {
            await _bridge.RequestAsync("workspace.add_folder", new { folder });
            var snapshot = await _bridge.RequestAsync("system.snapshot", new { });
            ApplySnapshot(snapshot);
            StatusText = "Trusted folder added and targets rediscovered";
        });
    }

    [RelayCommand]
    private async Task CheckProjectAsync(LocalTestingProjectItemViewModel? project)
    {
        if (project is null)
        {
            return;
        }
        await RunActionAsync(
            $"Checking {project.Name}…",
            "workspace.action",
            new { project = project.Name, action = "check" },
            $"{project.Name} preflight checked");
    }

    [RelayCommand]
    private async Task BuildTargetAsync(LocalTestingTargetItemViewModel? target)
    {
        if (target is null || !target.CanBuild)
        {
            return;
        }
        if (target.Kind.Equals("web", StringComparison.OrdinalIgnoreCase))
        {
            await RunActionAsync(
                $"Starting {target.Project} / {target.Service}…",
                "workspace.action",
                new { project = target.Project, service = target.Service, action = "start" },
                $"{target.Label} service started");
            return;
        }
        await RunActionAsync(
            $"Building {target.Project} / {target.Label}…",
            "target.action",
            new { project = target.Project, target_id = target.Id, action = "build" },
            $"{target.Label} build finished");
    }

    [RelayCommand]
    private async Task OpenTargetAsync(LocalTestingTargetItemViewModel? target)
    {
        if (target is null || !target.CanOpen)
        {
            return;
        }
        var serial = target.RequiresAndroidDevice ? ReadyDeviceSerial() : null;
        if (target.RequiresAndroidDevice && serial is null)
        {
            return;
        }
        await RunActionAsync(
            $"Opening {target.Project} / {target.Label}…",
            "target.action",
            new
            {
                project = target.Project,
                target_id = target.Id,
                action = "open",
                device_serial = serial
            },
            $"{target.Label} opened");
    }

    [RelayCommand]
    private async Task StopTargetAsync(LocalTestingTargetItemViewModel? target)
    {
        if (target is null || !target.CanStopAndroid)
        {
            return;
        }
        var serial = target.RequiresAndroidDevice ? ReadyDeviceSerial() : null;
        if (target.RequiresAndroidDevice && serial is null)
        {
            return;
        }
        var label = serial is not null ? $"{target.Label} on {serial}" : target.Label;
        await RunActionAsync(
            $"Stopping {target.Project} / {label}…",
            "target.action",
            new
            {
                project = target.Project,
                target_id = target.Id,
                action = "stop",
                device_serial = serial
            },
            $"{target.Label} stopped");
    }

    [RelayCommand]
    private async Task CaptureTargetLogsAsync(LocalTestingTargetItemViewModel? target)
    {
        if (target is null || !target.CanReadAndroidLogs)
        {
            return;
        }
        var serial = ReadyDeviceSerial();
        if (serial is null)
        {
            return;
        }
        await RunAsync($"Capturing bounded app logcat for {target.Label}…", async () =>
        {
            var result = await _bridge.RequestAsync("target.action", new
            {
                project = target.Project,
                target_id = target.Id,
                action = "logs",
                device_serial = serial
            });
            var crashCount = Int(result, "crash_anr_count");
            StatusText = crashCount > 0
                ? $"{target.Label} logs captured · {crashCount} crash/ANR evidence line{(crashCount == 1 ? "" : "s")}"
                : $"{target.Label} logs captured · no crash/ANR marker in bounded tail";
        });
    }

    [RelayCommand]
    private async Task StartServiceAsync(LocalTestingServiceItemViewModel? service)
    {
        if (service is null)
        {
            return;
        }
        await ServiceActionAsync(service, "start", "started");
    }

    [RelayCommand]
    private async Task RestartServiceAsync(LocalTestingServiceItemViewModel? service)
    {
        if (service is null)
        {
            return;
        }
        await ServiceActionAsync(service, "restart", "restarted");
    }

    [RelayCommand]
    private async Task StopServiceAsync(LocalTestingServiceItemViewModel? service)
    {
        if (service is null)
        {
            return;
        }
        await ServiceActionAsync(service, "stop", "stopped");
    }

    [RelayCommand]
    private async Task CaptureServiceLogsAsync(LocalTestingServiceItemViewModel? service)
    {
        if (service is null)
        {
            return;
        }
        await RunActionAsync(
            $"Capturing {service.Project} / {service.Name} logs…",
            "workspace.action",
            new { project = service.Project, service = service.Name, action = "logs", lines = 120 },
            $"{service.Name} logs captured");
    }

    private string? ReadyDeviceSerial()
    {
        if (SelectedDevice is not null && SelectedDevice.Ready)
        {
            return SelectedDevice.Serial;
        }
        SetError("Choose one ready Android device before using this Android target action.");
        return null;
    }

    private Task ServiceActionAsync(LocalTestingServiceItemViewModel service, string action, string completed) =>
        RunActionAsync(
            $"{char.ToUpperInvariant(action[0])}{action[1..]} {service.Project} / {service.Name}…",
            "workspace.action",
            new { project = service.Project, service = service.Name, action },
            $"{service.Name} {completed}");

    private async Task RunActionAsync(string running, string method, object payload, string completed)
    {
        await RunAsync(running, async () =>
        {
            await _bridge.RequestAsync(method, payload);
            var snapshot = await _bridge.RequestAsync("system.snapshot", new { });
            ApplySnapshot(snapshot);
            StatusText = completed;
        });
    }

    private async Task RunAsync(string running, Func<Task> action)
    {
        if (IsBusy)
        {
            return;
        }
        try
        {
            IsBusy = true;
            ErrorMessage = string.Empty;
            OnPropertyChanged(nameof(HasError));
            StatusText = running;
            await action();
        }
        catch (Exception ex)
        {
            SetError(ex.Message);
            StatusText = "Local Testing action failed";
        }
        finally
        {
            IsBusy = false;
        }
    }

    private void SetError(string message)
    {
        ErrorMessage = message;
        OnPropertyChanged(nameof(HasError));
    }

    private void ApplySnapshot(JsonElement snapshot)
    {
        var selectedSerial = SelectedDevice?.Serial;
        Projects.Clear();
        Targets.Clear();
        Services.Clear();
        Devices.Clear();

        TrustedProjectCount = 0;
        RunnableTargetCount = 0;
        RunningServiceCount = 0;
        ReadyDeviceCount = 0;

        if (snapshot.TryGetProperty("local_testing", out var localTesting))
        {
            if (localTesting.TryGetProperty("workspace", out var workspace))
            {
                TrustedProjectCount = Int(workspace, "project_count");
                RunnableTargetCount = Int(workspace, "local_target_count");
                RunningServiceCount = Int(workspace, "running_service_count");
                if (workspace.TryGetProperty("projects", out var projects) && projects.ValueKind == JsonValueKind.Array)
                {
                    foreach (var project in projects.EnumerateArray())
                    {
                        AddProject(project);
                    }
                }
            }
            if (localTesting.TryGetProperty("adb", out var adb))
            {
                ReadyDeviceCount = Int(adb, "ready_count");
                AdbState = Text(adb, "status");
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

        if (snapshot.TryGetProperty("account", out var account))
        {
        }

        SelectedDevice = !string.IsNullOrWhiteSpace(selectedSerial)
            ? Devices.FirstOrDefault(row => string.Equals(row.Serial, selectedSerial, StringComparison.Ordinal))
            : Devices.FirstOrDefault(row => row.Ready);
        SelectedDevice ??= Devices.FirstOrDefault(row => row.Ready);

        OnPropertyChanged(nameof(HasProjects));
        OnPropertyChanged(nameof(HasTargets));
        OnPropertyChanged(nameof(HasServices));
        OnPropertyChanged(nameof(HasDevices));
    }

    private void AddProject(JsonElement project)
    {
        var name = Text(project, "project");
        var state = Text(project, "state");
        var root = Text(project, "root");
        var problem = Text(project, "error_type");
        if (string.IsNullOrWhiteSpace(problem))
        {
            problem = Text(project, "target_error_type");
        }

        var targetCount = 0;
        if (project.TryGetProperty("targets", out var targets) && targets.ValueKind == JsonValueKind.Array)
        {
            targetCount = targets.GetArrayLength();
            foreach (var target in targets.EnumerateArray())
            {
                var service = Text(target, "service");
                var ready = Bool(target, "ready");
                var running = Bool(target, "process_alive");
                Targets.Add(new LocalTestingTargetItemViewModel(
                    name,
                    Text(target, "id"),
                    Text(target, "label"),
                    Text(target, "kind"),
                    Text(target, "runtime"),
                    Text(target, "build_label"),
                    Text(target, "open_label"),
                    Text(target, "evidence"),
                    service,
                    ready,
                    running));
            }
        }

        var serviceCount = 0;
        if (project.TryGetProperty("services", out var services) && services.ValueKind == JsonValueKind.Array)
        {
            serviceCount = services.GetArrayLength();
            foreach (var service in services.EnumerateArray())
            {
                var ready = Bool(service, "ready");
                var running = Bool(service, "process_alive");
                var serviceState = Text(service, "state");
                Services.Add(new LocalTestingServiceItemViewModel(
                    name,
                    Text(service, "service"),
                    serviceState,
                    Text(service, "pid"),
                    RenderPorts(service),
                    Text(service, "open_url"),
                    Text(service, "health_url"),
                    ready,
                    running));
            }
        }

        Projects.Add(new LocalTestingProjectItemViewModel(
            name,
            root,
            state,
            targetCount,
            serviceCount,
            problem));
    }

    private static string RenderPorts(JsonElement row)
    {
        if (!row.TryGetProperty("ports", out var ports))
        {
            return string.Empty;
        }
        if (ports.ValueKind != JsonValueKind.Array)
        {
            return ports.ToString();
        }
        var values = new List<string>();
        foreach (var port in ports.EnumerateArray())
        {
            if (port.ValueKind == JsonValueKind.Object)
            {
                var host = Text(port, "host");
                var number = Text(port, "port");
                var open = Bool(port, "open") ? "open" : "closed";
                values.Add($"{(string.IsNullOrWhiteSpace(host) ? "127.0.0.1" : host)}:{number} {open}");
            }
            else
            {
                values.Add(port.ToString());
            }
        }
        return string.Join(", ", values);
    }

    private static string Text(JsonElement value, string property)
    {
        return value.TryGetProperty(property, out var item) && item.ValueKind != JsonValueKind.Null
            ? item.ToString()
            : string.Empty;
    }

    private static int Int(JsonElement value, string property)
    {
        return value.TryGetProperty(property, out var item) && item.TryGetInt32(out var result) ? result : 0;
    }

    private static bool Bool(JsonElement value, string property)
    {
        return value.TryGetProperty(property, out var item) && item.ValueKind == JsonValueKind.True;
    }
}
