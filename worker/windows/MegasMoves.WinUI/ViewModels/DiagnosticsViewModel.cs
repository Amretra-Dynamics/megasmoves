using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using MegasMoves.Services;

namespace MegasMoves.ViewModels;

public partial class DiagnosticsViewModel : ObservableObject
{
    private readonly MegasCoreBridge _bridge;

    [ObservableProperty] private string rawLogTitle = "Select a declared service";
    [ObservableProperty] private string rawLogText = string.Empty;
    [ObservableProperty] private string displayedRawLogText = string.Empty;
    [ObservableProperty] private string rawLogSearch = string.Empty;
    [ObservableProperty] private int rawLogLines;
    [ObservableProperty] private bool isBusy;
    [ObservableProperty] private bool isFollowingRawLogs;
    [ObservableProperty] private string rawLogStatus = "Not following";
    [ObservableProperty] private string errorMessage = string.Empty;

    public DiagnosticsViewModel(MegasCoreBridge bridge)
    {
        _bridge = bridge;
    }

    public bool HasError => !string.IsNullOrWhiteSpace(ErrorMessage);

    public async Task LoadRawLogsAsync(RuntimeServiceViewModel service, int lines = 500)
    {
        ArgumentNullException.ThrowIfNull(service);
        var boundedLines = Math.Clamp(lines, 1, 500);

        try
        {
            IsBusy = true;
            ErrorMessage = string.Empty;
            OnPropertyChanged(nameof(HasError));
            RawLogTitle = $"{service.Project} / {service.Service}";

            var result = await _bridge.RequestAsync(
                "workspace.action",
                new
                {
                    action = "logs",
                    project = service.Project,
                    service = service.Service,
                    lines = boundedLines
                });

            ApplyRawResult(result);
            RawLogStatus = "Snapshot loaded";
        }
        catch (Exception ex)
        {
            SetRawLogError(ex.Message, "Raw-log snapshot failed");
        }
        finally
        {
            IsBusy = false;
        }
    }

    public async Task FollowRawLogsAsync(
        RuntimeServiceViewModel service,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(service);
        IsFollowingRawLogs = true;
        RawLogTitle = $"{service.Project} / {service.Service}";
        RawLogStatus = "Connecting live log stream…";

        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                try
                {
                    await foreach (var frame in _bridge.SubscribeRawLogsAsync(
                        service.Project,
                        service.Service,
                        lines: 500,
                        cancellationToken))
                    {
                        var changed = frame.TryGetProperty("changed", out var changedValue) &&
                            changedValue.ValueKind == JsonValueKind.True;
                        var initial = frame.TryGetProperty("initial", out var initialValue) &&
                            initialValue.ValueKind == JsonValueKind.True;
                        if ((changed || initial) &&
                            frame.TryGetProperty("result", out var result) &&
                            result.ValueKind == JsonValueKind.Object)
                        {
                            ApplyRawResult(result);
                        }

                        ErrorMessage = string.Empty;
                        OnPropertyChanged(nameof(HasError));
                        RawLogStatus = $"Live · {RawLogLines} bounded line{(RawLogLines == 1 ? "" : "s")}";
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
                    RawLogStatus = "Live log stream reconnecting…";
                    await Task.Delay(TimeSpan.FromMilliseconds(250), cancellationToken);
                }
            }
        }
        finally
        {
            IsFollowingRawLogs = false;
            if (cancellationToken.IsCancellationRequested)
            {
                RawLogStatus = "Follow off";
            }
        }
    }

    public void ApplySearch()
    {
        var needle = RawLogSearch.Trim();
        if (string.IsNullOrWhiteSpace(needle))
        {
            DisplayedRawLogText = RawLogText;
            return;
        }

        DisplayedRawLogText = string.Join(
            Environment.NewLine,
            RawLogText
                .Split(new[] { "\r\n", "\n" }, StringSplitOptions.None)
                .Where(line => line.Contains(needle, StringComparison.OrdinalIgnoreCase)));
    }

    public void ClearSearch()
    {
        RawLogSearch = string.Empty;
        DisplayedRawLogText = RawLogText;
    }

    private void ApplyRawResult(JsonElement result)
    {
        RawLogText = Text(result, "text");
        RawLogLines = Int(result, "lines");
        ApplySearch();
        ErrorMessage = string.Empty;
        OnPropertyChanged(nameof(HasError));
    }

    private void SetRawLogError(string message, string status)
    {
        ErrorMessage = message;
        RawLogText = string.Empty;
        DisplayedRawLogText = string.Empty;
        RawLogLines = 0;
        RawLogStatus = status;
        OnPropertyChanged(nameof(HasError));
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
}
