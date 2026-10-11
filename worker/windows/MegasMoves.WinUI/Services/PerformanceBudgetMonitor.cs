namespace MegasMoves.Services;

public sealed record PerformanceBudgetSnapshot(
    double? WarmLaunchMs,
    double? ControlRoundTripP95Ms,
    int ControlRoundTripSamples,
    double? ActivityVisibilityP95Ms,
    int ActivityVisibilitySamples,
    double? CoreRecoveryMs)
{
    public string Render()
    {
        static string Metric(string name, double? value, double target, string qualifier = "")
        {
            if (value is null)
            {
                return $"{name}: not measured{qualifier}";
            }
            var verdict = value.Value <= target ? "within target" : "over target";
            return $"{name}: {value.Value:0.0} ms / ≤ {target:0} ms ({verdict}){qualifier}";
        }

        return string.Join(
            Environment.NewLine,
            Metric("Warm launch → usable Overview", WarmLaunchMs, PerformanceBudgetMonitor.WarmLaunchBudgetMs),
            Metric(
                "Local control round-trip p95",
                ControlRoundTripP95Ms,
                PerformanceBudgetMonitor.ControlRoundTripBudgetMs,
                ControlRoundTripSamples > 0 ? $" · n={ControlRoundTripSamples}" : string.Empty),
            Metric(
                "MCP event → visible Activity p95",
                ActivityVisibilityP95Ms,
                PerformanceBudgetMonitor.ActivityVisibilityBudgetMs,
                ActivityVisibilitySamples > 0 ? $" · n={ActivityVisibilitySamples}" : string.Empty),
            Metric("MCP restart → authoritative state", CoreRecoveryMs, PerformanceBudgetMonitor.CoreRecoveryBudgetMs));
    }
}

/// <summary>
/// Bounded in-memory measurements for the engineering budgets in the Windows
/// production book. A missing measurement stays "not measured"; this service
/// never manufactures a pass result from configuration or target values.
/// </summary>
public sealed class PerformanceBudgetMonitor
{
    public const double WarmLaunchBudgetMs = 1500;
    public const double ControlRoundTripBudgetMs = 100;
    public const double ActivityVisibilityBudgetMs = 100;
    public const double CoreRecoveryBudgetMs = 5000;
    public const int MaxSamples = 512;

    private readonly object _gate = new();
    private readonly Queue<double> _controlRoundTrips = new();
    private readonly Queue<double> _activityVisibility = new();
    private double? _warmLaunchMs;
    private double? _coreRecoveryMs;

    public void MarkWarmLaunch(TimeSpan elapsed)
    {
        var value = Math.Max(0, elapsed.TotalMilliseconds);
        lock (_gate)
        {
            _warmLaunchMs ??= value;
        }
    }

    public void RecordControlRoundTrip(TimeSpan elapsed) =>
        Add(_controlRoundTrips, elapsed.TotalMilliseconds);

    public void RecordActivityVisibility(TimeSpan elapsed) =>
        Add(_activityVisibility, elapsed.TotalMilliseconds);

    public void RecordCoreRecovery(TimeSpan elapsed)
    {
        lock (_gate)
        {
            _coreRecoveryMs = Math.Max(0, elapsed.TotalMilliseconds);
        }
    }

    public PerformanceBudgetSnapshot Snapshot()
    {
        lock (_gate)
        {
            return new PerformanceBudgetSnapshot(
                _warmLaunchMs,
                Percentile95(_controlRoundTrips),
                _controlRoundTrips.Count,
                Percentile95(_activityVisibility),
                _activityVisibility.Count,
                _coreRecoveryMs);
        }
    }

    private void Add(Queue<double> samples, double milliseconds)
    {
        if (!double.IsFinite(milliseconds))
        {
            return;
        }
        lock (_gate)
        {
            samples.Enqueue(Math.Max(0, milliseconds));
            while (samples.Count > MaxSamples)
            {
                samples.Dequeue();
            }
        }
    }

    private static double? Percentile95(IEnumerable<double> samples)
    {
        var values = samples.OrderBy(value => value).ToArray();
        if (values.Length == 0)
        {
            return null;
        }
        var index = Math.Clamp(
            (int)Math.Ceiling(values.Length * 0.95) - 1,
            0,
            values.Length - 1);
        return values[index];
    }
}
