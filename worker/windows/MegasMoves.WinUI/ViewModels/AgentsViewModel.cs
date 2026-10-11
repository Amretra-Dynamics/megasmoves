using System.Collections.ObjectModel;
using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MegasMoves.Services;

namespace MegasMoves.ViewModels;

public sealed record AgentWorkbenchItemViewModel(
    string Client,
    string DisplayName,
    string Detection,
    string Connection,
    string InstallState,
    string Version,
    string Repository,
    bool Detected,
    bool Connected,
    bool Downloadable,
    bool ManagedInstalled)
{
    public bool CanInstall => Downloadable && !ManagedInstalled;
    public bool CanConnect => Detected && !Connected;
    public bool CanDisconnect => Detected && Connected;
    public bool CanUninstall => ManagedInstalled;
    public string PrimaryState => Connected ? "Connected" : ManagedInstalled ? "Installed" : Detected ? "Detected" : Downloadable ? "Downloadable" : "Unavailable";
}

public sealed record AgentTaskItemViewModel(
    string Id,
    string Title,
    string Project,
    string State,
    string AssignedClient,
    int AssignmentGeneration)
{
    public string Assignment => string.IsNullOrWhiteSpace(AssignedClient) ? "Unassigned" : $"Assigned to {AssignedClient}";
    public string ProjectLabel => string.IsNullOrWhiteSpace(Project) ? "No project binding" : Project;
    public bool IsAssigned => !string.IsNullOrWhiteSpace(AssignedClient);
}

public partial class AgentsViewModel : ObservableObject
{
    private readonly MegasCoreBridge _bridge;

    [ObservableProperty] private bool isBusy;
    [ObservableProperty] private string statusText = "Loading agent state…";
    [ObservableProperty] private string errorMessage = string.Empty;
    [ObservableProperty] private AgentWorkbenchItemViewModel? selectedAgent;
    [ObservableProperty] private AgentTaskItemViewModel? selectedTask;
    [ObservableProperty] private string newTaskTitle = string.Empty;
    [ObservableProperty] private string newTaskProject = string.Empty;

    public AgentsViewModel(MegasCoreBridge bridge)
    {
        _bridge = bridge;
    }

    public ObservableCollection<AgentWorkbenchItemViewModel> Agents { get; } = [];
    public ObservableCollection<AgentTaskItemViewModel> Tasks { get; } = [];

    public bool HasError => !string.IsNullOrWhiteSpace(ErrorMessage);
    public bool HasTasks => Tasks.Count > 0;

    public async Task InitializeAsync()
    {
        if (Agents.Count == 0 && !IsBusy)
        {
            await RefreshAsync();
        }
    }

    [RelayCommand]
    private async Task RefreshAsync()
    {
        await RunAsync("Refreshing agents and tasks…", async () =>
        {
            var agents = await _bridge.RequestAsync("agents.status", new { });
            var tasks = await _bridge.RequestAsync("tasks.list", new { include_terminal = true });
            ApplyAgents(agents);
            ApplyTasks(tasks);
            StatusText = $"{Agents.Count} agent clients • {Tasks.Count} tasks";
        });
    }

    [RelayCommand]
    private async Task InstallAsync(AgentWorkbenchItemViewModel? agent)
    {
        if (agent is null || !agent.CanInstall)
        {
            return;
        }
        await RunAsync($"Downloading {agent.DisplayName}…", async () =>
        {
            await _bridge.RequestAsync("agent.install", new
            {
                client = agent.Client,
                confirm = true
            });
            await RefreshCoreAsync();
            StatusText = $"{agent.DisplayName} installed, verified and connected to Megas MCP";
        });
    }

    [RelayCommand]
    private async Task ConnectAsync(AgentWorkbenchItemViewModel? agent)
    {
        if (agent is null || !agent.CanConnect)
        {
            return;
        }
        await RunAsync($"Connecting {agent.DisplayName}…", async () =>
        {
            await _bridge.RequestAsync("agent.connect", new
            {
                client = agent.Client,
                confirm = true
            });
            await RefreshCoreAsync();
            StatusText = $"{agent.DisplayName} connected to embedded Megas MCP";
        });
    }

    [RelayCommand]
    private async Task DisconnectAsync(AgentWorkbenchItemViewModel? agent)
    {
        if (agent is null || !agent.CanDisconnect)
        {
            return;
        }
        await RunAsync($"Disconnecting {agent.DisplayName}…", async () =>
        {
            await _bridge.RequestAsync("agent.disconnect", new
            {
                client = agent.Client,
                confirm = true
            });
            await RefreshCoreAsync();
            StatusText = $"{agent.DisplayName} disconnected. Existing tasks were preserved.";
        });
    }

    [RelayCommand]
    private async Task UninstallAsync(AgentWorkbenchItemViewModel? agent)
    {
        if (agent is null || !agent.CanUninstall)
        {
            return;
        }
        await RunAsync($"Removing managed {agent.DisplayName}…", async () =>
        {
            await _bridge.RequestAsync("agent.uninstall", new
            {
                client = agent.Client,
                confirm = true
            });
            await RefreshCoreAsync();
            StatusText = $"Managed {agent.DisplayName} removed. Task records remain intact.";
        });
    }

    [RelayCommand]
    private async Task DownloadAndAssignAsync()
    {
        var agent = SelectedAgent;
        var title = NewTaskTitle.Trim();
        var project = NewTaskProject.Trim();
        if (agent is null)
        {
            SetError("Choose an agent first.");
            return;
        }
        if (string.IsNullOrWhiteSpace(title))
        {
            SetError("Enter a task before downloading and assigning the agent.");
            return;
        }

        await RunAsync($"Preparing {agent.DisplayName} and assigning task…", async () =>
        {
            if (agent.Downloadable)
            {
                await _bridge.RequestAsync("agent.download_and_assign", new
                {
                    client = agent.Client,
                    title,
                    project = string.IsNullOrWhiteSpace(project) ? null : project,
                    confirm = true
                });
            }
            else
            {
                if (!agent.Detected)
                {
                    throw new InvalidOperationException("This agent is not installed and is not in the reviewed download catalog.");
                }
                var created = await _bridge.RequestAsync("task.create", new
                {
                    title,
                    project = string.IsNullOrWhiteSpace(project) ? null : project
                });
                var taskId = Text(created, "id");
                await _bridge.RequestAsync("task.assign", new
                {
                    task_id = taskId,
                    client = agent.Client
                });
            }
            NewTaskTitle = string.Empty;
            NewTaskProject = string.Empty;
            await RefreshCoreAsync();
            StatusText = $"Task assigned to {agent.DisplayName}";
        });
    }

    [RelayCommand]
    private async Task AssignSelectedTaskAsync()
    {
        var task = SelectedTask;
        var agent = SelectedAgent;
        if (task is null || agent is null)
        {
            SetError("Choose both a task and an agent.");
            return;
        }

        await RunAsync($"Assigning task to {agent.DisplayName}…", async () =>
        {
            if (!agent.Detected && agent.Downloadable)
            {
                await _bridge.RequestAsync("agent.download_and_assign", new
                {
                    client = agent.Client,
                    task_id = task.Id,
                    confirm = true
                });
            }
            else
            {
                if (!agent.Detected)
                {
                    throw new InvalidOperationException("The selected agent is not available on this machine.");
                }
                if (!agent.Connected)
                {
                    await _bridge.RequestAsync("agent.connect", new
                    {
                        client = agent.Client,
                        confirm = true
                    });
                }
                await _bridge.RequestAsync("task.assign", new
                {
                    task_id = task.Id,
                    client = agent.Client
                });
            }
            await RefreshCoreAsync();
            StatusText = $"{task.Title} assigned to {agent.DisplayName}";
        });
    }

    [RelayCommand]
    private async Task UnassignSelectedTaskAsync()
    {
        var task = SelectedTask;
        if (task is null)
        {
            SetError("Choose a task first.");
            return;
        }
        await RunAsync("Removing agent assignment…", async () =>
        {
            await _bridge.RequestAsync("task.unassign", new { task_id = task.Id });
            await RefreshCoreAsync();
            StatusText = $"{task.Title} is unassigned and still preserved";
        });
    }

    private async Task RefreshCoreAsync()
    {
        var selectedClient = SelectedAgent?.Client;
        var selectedTaskId = SelectedTask?.Id;
        var agents = await _bridge.RequestAsync("agents.status", new { });
        var tasks = await _bridge.RequestAsync("tasks.list", new { include_terminal = true });
        ApplyAgents(agents);
        ApplyTasks(tasks);
        SelectedAgent = string.IsNullOrWhiteSpace(selectedClient)
            ? Agents.FirstOrDefault()
            : Agents.FirstOrDefault(item => string.Equals(item.Client, selectedClient, StringComparison.OrdinalIgnoreCase)) ?? Agents.FirstOrDefault();
        SelectedTask = string.IsNullOrWhiteSpace(selectedTaskId)
            ? Tasks.FirstOrDefault()
            : Tasks.FirstOrDefault(item => string.Equals(item.Id, selectedTaskId, StringComparison.Ordinal)) ?? Tasks.FirstOrDefault();
    }

    private async Task RunAsync(string status, Func<Task> action)
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
            StatusText = status;
            await action();
        }
        catch (Exception ex)
        {
            SetError(ex.Message);
            StatusText = "Action failed";
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

    private void ApplyAgents(JsonElement rows)
    {
        Agents.Clear();
        if (rows.ValueKind != JsonValueKind.Array)
        {
            SelectedAgent = null;
            return;
        }

        foreach (var row in rows.EnumerateArray())
        {
            var client = Text(row, "client");
            var detected = Bool(row, "detected");
            var connected = NullableBool(row, "megas_configured") is true;
            var downloadable = Bool(row, "downloadable");
            var displayName = client;
            var repository = string.Empty;
            var managedInstalled = false;
            var version = string.Empty;

            if (row.TryGetProperty("catalog", out var catalog) && catalog.ValueKind == JsonValueKind.Object)
            {
                displayName = Text(catalog, "display_name");
                repository = Text(catalog, "repository");
            }
            if (string.IsNullOrWhiteSpace(displayName))
            {
                displayName = client;
            }
            if (row.TryGetProperty("managed_install", out var managed) && managed.ValueKind == JsonValueKind.Object)
            {
                managedInstalled = Bool(managed, "installed") && Bool(managed, "verified");
                version = Text(managed, "version");
                if (string.IsNullOrWhiteSpace(repository))
                {
                    repository = Text(managed, "repository");
                }
            }

            var installState = managedInstalled
                ? "Managed install verified"
                : detected
                    ? "Detected on this machine"
                    : downloadable
                        ? "Available from reviewed GitHub release"
                        : "Not detected";
            Agents.Add(new AgentWorkbenchItemViewModel(
                client,
                displayName,
                detected ? "Detected" : "Not detected",
                connected ? "Megas MCP connected" : "Not connected",
                installState,
                version,
                repository,
                detected,
                connected,
                downloadable,
                managedInstalled));
        }

        if (SelectedAgent is null || !Agents.Any(item => string.Equals(item.Client, SelectedAgent.Client, StringComparison.OrdinalIgnoreCase)))
        {
            SelectedAgent = Agents.FirstOrDefault(item => item.Downloadable) ?? Agents.FirstOrDefault();
        }
    }

    private void ApplyTasks(JsonElement rows)
    {
        Tasks.Clear();
        if (rows.ValueKind != JsonValueKind.Array)
        {
            SelectedTask = null;
            OnPropertyChanged(nameof(HasTasks));
            return;
        }
        foreach (var row in rows.EnumerateArray())
        {
            Tasks.Add(new AgentTaskItemViewModel(
                Text(row, "id"),
                Text(row, "title"),
                Text(row, "project"),
                Text(row, "state"),
                Text(row, "assigned_client"),
                Int(row, "assignment_generation")));
        }
        if (SelectedTask is null || !Tasks.Any(item => string.Equals(item.Id, SelectedTask.Id, StringComparison.Ordinal)))
        {
            SelectedTask = Tasks.FirstOrDefault();
        }
        OnPropertyChanged(nameof(HasTasks));
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

    private static bool? NullableBool(JsonElement value, string property)
    {
        if (!value.TryGetProperty(property, out var item) || item.ValueKind == JsonValueKind.Null)
        {
            return null;
        }
        return item.ValueKind == JsonValueKind.True;
    }
}
