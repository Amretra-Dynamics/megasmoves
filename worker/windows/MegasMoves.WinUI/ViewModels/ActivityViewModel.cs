using System.Collections.ObjectModel;
using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MegasMoves.Services;

namespace MegasMoves.ViewModels;

public partial class ActivityViewModel : ObservableObject
{
    private readonly MegasCoreBridge _bridge;
    private readonly PerformanceBudgetMonitor _performance;
    private string _followCursor = string.Empty;

    [ObservableProperty] private string query = string.Empty;
    [ObservableProperty] private string selectedSource = "All";
    [ObservableProperty] private bool errorsOnly;
    [ObservableProperty] private bool isBusy;
    [ObservableProperty] private bool isFollowing;
    [ObservableProperty] private string errorMessage = string.Empty;
    [ObservableProperty] private string statusText = "Activity not loaded";

    public ActivityViewModel(MegasCoreBridge bridge, PerformanceBudgetMonitor performance)
    {
        _bridge = bridge;
        _performance = performance;
        RefreshCommand = new AsyncRelayCommand(RefreshAsync);
        ClearFiltersCommand = new AsyncRelayCommand(ClearFiltersAsync);
    }

    public ObservableCollection<ActivityItemViewModel> Rows { get; } = [];

    public IReadOnlyList<string> Sources { get; } =
        ["All", "Agent", "MCP", "MegasMoves", "Runtime", "Test", "Network"];

    public IAsyncRelayCommand RefreshCommand { get; }
    public IAsyncRelayCommand ClearFiltersCommand { get; }
    public bool HasError => !string.IsNullOrWhiteSpace(ErrorMessage);
    public bool HasRows => Rows.Count > 0;

    public async Task RefreshAsync()
    {
        try
        {
            IsBusy = true;
            ErrorMessage = string.Empty;
            OnPropertyChanged(nameof(HasError));

            var result = await _bridge.RequestAsync(
                "activity.list",
                new
                {
                    limit = 500,
                    source = SelectedSourceValue(),
                    errors_only = ErrorsOnly,
                    query = Query.Trim()
                });

            ApplyRows(result);
            StatusText = $"{Rows.Count} redacted structured event{(Rows.Count == 1 ? "" : "s")}";
        }
        catch (Exception ex)
        {
            ErrorMessage = ex.Message;
            StatusText = "Activity refresh failed";
            OnPropertyChanged(nameof(HasError));
        }
        finally
        {
            IsBusy = false;
        }
    }

    public async Task FollowAsync(CancellationToken cancellationToken)
    {
        IsFollowing = true;
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                try
                {
                    await foreach (var frame in _bridge.SubscribeActivityAsync(
                        _followCursor,
                        SelectedSourceValue(),
                        ErrorsOnly,
                        Query.Trim(),
                        limit: 500,
                        cancellationToken: cancellationToken))
                    {
                        if (frame.TryGetProperty("cursor", out var cursor) &&
                            cursor.ValueKind == JsonValueKind.String)
                        {
                            _followCursor = cursor.GetString() ?? _followCursor;
                        }

                        var initial = frame.TryGetProperty("initial", out var initialValue) &&
                            initialValue.ValueKind == JsonValueKind.True;
                        var changed = frame.TryGetProperty("changed", out var changedValue) &&
                            changedValue.ValueKind == JsonValueKind.True;

                        if ((initial || changed) &&
                            frame.TryGetProperty("rows", out var rows) &&
                            rows.ValueKind == JsonValueKind.Array)
                        {
                            ApplyRows(rows);
                            if (changed && !initial)
                            {
                                RecordVisibilityLatency(rows);
                            }
                        }

                        ErrorMessage = string.Empty;
                        OnPropertyChanged(nameof(HasError));
                        StatusText = $"Live · {Rows.Count} structured event{(Rows.Count == 1 ? "" : "s")}";
                    }
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    break;
                }
                catch (Exception ex) when (ex is IOException or InvalidOperationException or JsonException)
                {
                    ErrorMessage = ex.Message;
                    OnPropertyChanged(nameof(HasError));
                    StatusText = "Activity reconnecting…";
                    await Task.Delay(TimeSpan.FromMilliseconds(250), cancellationToken);
                }
            }
        }
        finally
        {
            IsFollowing = false;
        }
    }

    public string ExportJson()
    {
        var rows = Rows.Select(row => new
        {
            time = row.Time,
            source = row.Source,
            severity = row.State,
            operation_id = row.Operation,
            message = row.Message,
            duration = row.Duration,
        });
        return JsonSerializer.Serialize(rows, new JsonSerializerOptions { WriteIndented = true });
    }

    public string CopyText(ActivityItemViewModel row)
    {
        return string.Join(
            "  ",
            new[]
            {
                row.Time,
                row.Source,
                row.State,
                row.Message,
                row.Operation,
                row.Duration,
            }.Where(value => !string.IsNullOrWhiteSpace(value)));
    }

    public async Task ClearFiltersAsync()
    {
        Query = string.Empty;
        SelectedSource = "All";
        ErrorsOnly = false;
        await RefreshAsync();
    }

    private string SelectedSourceValue() =>
        string.Equals(SelectedSource, "All", StringComparison.Ordinal)
            ? string.Empty
            : SelectedSource;

    private void ApplyRows(JsonElement rows)
    {
        Rows.Clear();
        if (rows.ValueKind == JsonValueKind.Array)
        {
            foreach (var row in rows.EnumerateArray())
            {
                Rows.Add(new ActivityItemViewModel(
                    Text(row, "at"),
                    Text(row, "source"),
                    Text(row, "severity"),
                    Text(row, "operation_id"),
                    Text(row, "message"),
                    row.TryGetProperty("duration_ms", out var duration) && duration.ValueKind == JsonValueKind.Number
                        ? $"{duration.GetDouble():0} ms"
                        : string.Empty));
            }
        }
        OnPropertyChanged(nameof(HasRows));
    }

    private void RecordVisibilityLatency(JsonElement rows)
    {
        if (rows.GetArrayLength() == 0)
        {
            return;
        }
        var newest = Text(rows[0], "at");
        if (!DateTimeOffset.TryParse(newest, out var eventAt))
        {
            return;
        }
        var elapsed = DateTimeOffset.UtcNow - eventAt.ToUniversalTime();
        if (elapsed >= TimeSpan.Zero && elapsed <= TimeSpan.FromSeconds(10))
        {
            _performance.RecordActivityVisibility(elapsed);
        }
    }

    private static string Text(JsonElement value, string property)
    {
        return value.TryGetProperty(property, out var item) && item.ValueKind != JsonValueKind.Null
            ? item.ToString()
            : string.Empty;
    }
}
