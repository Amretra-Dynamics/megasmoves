using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text.Json;
using MegasMoves.Native;

namespace MegasMoves.Services;

public sealed class MegasCoreBridge : IAsyncDisposable
{
    private const int MaxFrameBytes = 256 * 1024;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly SemaphoreSlim _activitySubscriptionGate = new(1, 1);
    private readonly SemaphoreSlim _logSubscriptionGate = new(1, 1);
    private readonly CancellationTokenSource _shutdown = new();
    private readonly MegasNativeRuntime _native;
    private readonly PerformanceBudgetMonitor _performance;
    private readonly JsonSerializerOptions _json = new(JsonSerializerDefaults.Web)
    {
        PropertyNameCaseInsensitive = true
    };

    private Process? _process;
    private NamedPipeClientStream? _pipe;
    private string? _pipeName;
    private string? _activityPipeName;
    private string? _logPipeName;
    private string? _authToken;
    private long _nextRequestId;
    private long _recoveryStartedTimestamp;
    private int _recoveryPending;
    private bool _disposed;

    public MegasCoreBridge(MegasNativeRuntime native, PerformanceBudgetMonitor performance)
    {
        _native = native;
        _performance = performance;
    }

    public event EventHandler<string>? DiagnosticMessage;

    public bool IsConnected =>
        !_disposed &&
        _process is { HasExited: false } &&
        _pipe is { IsConnected: true } &&
        !string.IsNullOrWhiteSpace(_authToken);

    public async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var ping = await RequestAsync("ping", new { }, cancellationToken);
        if (!ping.TryGetProperty("ok", out var ok) || !ok.GetBoolean())
        {
            throw new InvalidOperationException("Megas capability core did not complete its authenticated startup handshake.");
        }
    }

    public async Task<JsonElement> RequestAsync(
        string method,
        object? parameters = null,
        CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        var timer = Stopwatch.StartNew();
        var gateHeld = false;
        var completed = false;
        try
        {
            await _gate.WaitAsync(cancellationToken);
            gateHeld = true;
            if (!IsConnected)
            {
                await StartWithoutLockAsync(cancellationToken);
            }

            var id = Interlocked.Increment(ref _nextRequestId);
            var request = JsonSerializer.SerializeToUtf8Bytes(new
            {
                id,
                auth = _authToken,
                method,
                @params = parameters ?? new { }
            }, _json);
            if (request.Length > MaxFrameBytes)
            {
                throw new InvalidOperationException("Megas desktop request exceeds the 256 KiB IPC bound.");
            }

            await WriteFrameAsync(_pipe!, request, cancellationToken);
            var response = await ReadFrameAsync(_pipe!, cancellationToken);

            using var document = JsonDocument.Parse(response);
            var root = document.RootElement;
            if (root.TryGetProperty("id", out var responseId) &&
                responseId.ValueKind == JsonValueKind.Number &&
                responseId.GetInt64() != id)
            {
                throw new InvalidOperationException("Megas capability core returned an out-of-order response.");
            }
            if (!root.TryGetProperty("ok", out var responseOk) || !responseOk.GetBoolean())
            {
                throw new InvalidOperationException(ResponseError(root, "Megas capability operation failed."));
            }

            JsonElement result;
            if (!root.TryGetProperty("result", out var resultElement))
            {
                result = JsonSerializer.SerializeToElement(new { }, _json);
            }
            else
            {
                result = resultElement.Clone();
            }
            completed = true;

            if (string.Equals(method, "system.snapshot", StringComparison.Ordinal) &&
                Interlocked.Exchange(ref _recoveryPending, 0) == 1)
            {
                var started = Interlocked.Exchange(ref _recoveryStartedTimestamp, 0);
                if (started > 0)
                {
                    _performance.RecordCoreRecovery(Stopwatch.GetElapsedTime(started));
                }
            }
            return result;
        }
        catch (IOException)
        {
            MarkRecoveryPending();
            if (gateHeld)
            {
                await ResetConnectionWithoutLockAsync();
            }
            throw;
        }
        finally
        {
            timer.Stop();
            if (gateHeld)
            {
                _gate.Release();
            }
            if (completed)
            {
                _performance.RecordControlRoundTrip(timer.Elapsed);
            }
        }
    }

    public async IAsyncEnumerable<JsonElement> SubscribeActivityAsync(
        string? cursor,
        string source,
        bool errorsOnly,
        string query,
        int limit = 300,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        var connection = await SubscriptionConnectionAsync(
            SubscriptionKind.Activity,
            cancellationToken);
        using var linkedCancellation = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken,
            _shutdown.Token);
        var token = linkedCancellation.Token;
        await _activitySubscriptionGate.WaitAsync(token);
        try
        {
            await using var activityPipe = await ConnectSubscriptionPipeAsync(
                connection.PipeName,
                "Activity",
                token);
            var request = JsonSerializer.SerializeToUtf8Bytes(new
            {
                auth = connection.AuthToken,
                cursor = cursor ?? string.Empty,
                source,
                errors_only = errorsOnly,
                query,
                limit = Math.Clamp(limit, 1, 500),
            }, _json);
            await WriteFrameAsync(activityPipe, request, token);

            while (!token.IsCancellationRequested)
            {
                var root = await ReadSubscriptionFrameAsync(activityPipe, "Activity", token);
                yield return root;
            }
        }
        finally
        {
            _activitySubscriptionGate.Release();
        }
    }

    public async IAsyncEnumerable<JsonElement> SubscribeRawLogsAsync(
        string project,
        string service,
        int lines = 500,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(project) || string.IsNullOrWhiteSpace(service))
        {
            throw new ArgumentException("Raw-log subscription requires project and service.");
        }

        var connection = await SubscriptionConnectionAsync(
            SubscriptionKind.RawLogs,
            cancellationToken);
        using var linkedCancellation = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken,
            _shutdown.Token);
        var token = linkedCancellation.Token;
        await _logSubscriptionGate.WaitAsync(token);
        try
        {
            await using var logPipe = await ConnectSubscriptionPipeAsync(
                connection.PipeName,
                "raw-log",
                token);
            var request = JsonSerializer.SerializeToUtf8Bytes(new
            {
                auth = connection.AuthToken,
                project = project.Trim(),
                service = service.Trim(),
                lines = Math.Clamp(lines, 1, 500),
            }, _json);
            await WriteFrameAsync(logPipe, request, token);

            while (!token.IsCancellationRequested)
            {
                var root = await ReadSubscriptionFrameAsync(logPipe, "raw-log", token);
                yield return root;
            }
        }
        finally
        {
            _logSubscriptionGate.Release();
        }
    }

    private enum SubscriptionKind
    {
        Activity,
        RawLogs,
    }

    private sealed record SubscriptionConnection(string PipeName, string AuthToken);

    private async Task<SubscriptionConnection> SubscriptionConnectionAsync(
        SubscriptionKind kind,
        CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            if (!IsConnected)
            {
                await StartWithoutLockAsync(cancellationToken);
            }

            var pipeName = kind switch
            {
                SubscriptionKind.Activity => _activityPipeName,
                SubscriptionKind.RawLogs => _logPipeName,
                _ => null,
            };
            if (string.IsNullOrWhiteSpace(pipeName))
            {
                throw new InvalidOperationException($"Megas {kind} subscription transport is unavailable.");
            }
            var authToken = _authToken
                ?? throw new InvalidOperationException($"Megas {kind} subscription authentication is unavailable.");
            return new SubscriptionConnection(pipeName, authToken);
        }
        finally
        {
            _gate.Release();
        }
    }

    private static async Task<NamedPipeClientStream> ConnectSubscriptionPipeAsync(
        string pipeName,
        string label,
        CancellationToken cancellationToken)
    {
        var pipe = new NamedPipeClientStream(
            ".",
            pipeName,
            PipeDirection.InOut,
            PipeOptions.Asynchronous);
        using var connectTimeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        connectTimeout.CancelAfter(TimeSpan.FromSeconds(20));
        try
        {
            await pipe.ConnectAsync(connectTimeout.Token);
            if (!pipe.IsConnected)
            {
                throw new IOException($"Unable to connect to the authenticated Megas {label} pipe.");
            }
            return pipe;
        }
        catch
        {
            await pipe.DisposeAsync();
            throw;
        }
    }

    private static async Task<JsonElement> ReadSubscriptionFrameAsync(
        NamedPipeClientStream pipe,
        string label,
        CancellationToken cancellationToken)
    {
        var response = await ReadFrameAsync(pipe, cancellationToken);
        using var document = JsonDocument.Parse(response);
        var root = document.RootElement;
        if (!root.TryGetProperty("ok", out var ok) || !ok.GetBoolean())
        {
            throw new InvalidOperationException(ResponseError(root, $"Megas {label} subscription failed."));
        }
        return root.Clone();
    }

    private static string ResponseError(JsonElement root, string fallback)
    {
        if (root.TryGetProperty("error", out var error) &&
            error.ValueKind == JsonValueKind.Object &&
            error.TryGetProperty("message", out var message) &&
            message.ValueKind == JsonValueKind.String)
        {
            return message.GetString() ?? fallback;
        }
        return fallback;
    }

    private void MarkRecoveryPending()
    {
        if (Interlocked.CompareExchange(ref _recoveryPending, 1, 0) == 0)
        {
            Interlocked.Exchange(ref _recoveryStartedTimestamp, Stopwatch.GetTimestamp());
        }
    }

    private async Task StartWithoutLockAsync(CancellationToken cancellationToken)
    {
        await ResetConnectionWithoutLockAsync();

        var corePath = RuntimePayloadResolver.ResolveCoreExecutable();
        if (!File.Exists(corePath))
        {
            throw new FileNotFoundException(
                "Embedded Megas capability core is missing. Rebuild with worker/scripts/build_desktop.py.",
                corePath);
        }

        _pipeName = $"megasmoves-{Environment.ProcessId}-{Guid.NewGuid():N}";
        _activityPipeName = _pipeName + "-activity";
        _logPipeName = _pipeName + "-logs";
        _authToken = Convert.ToBase64String(RandomNumberGenerator.GetBytes(32));

        var startInfo = new ProcessStartInfo
        {
            FileName = corePath,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = false,
            RedirectStandardOutput = false,
            RedirectStandardError = true,
            WorkingDirectory = Path.GetDirectoryName(corePath) ?? AppContext.BaseDirectory,
        };
        startInfo.ArgumentList.Add("--pipe");
        startInfo.ArgumentList.Add(_pipeName);
        startInfo.ArgumentList.Add("--activity-pipe");
        startInfo.ArgumentList.Add(_activityPipeName);
        startInfo.ArgumentList.Add("--log-pipe");
        startInfo.ArgumentList.Add(_logPipeName);
        startInfo.Environment["MEGAS_DESKTOP_IPC_TOKEN"] = _authToken;

        _process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        _process.ErrorDataReceived += (_, args) =>
        {
            if (!string.IsNullOrWhiteSpace(args.Data))
            {
                DiagnosticMessage?.Invoke(this, args.Data);
            }
        };
        _process.Exited += (_, _) =>
        {
            MarkRecoveryPending();
            DiagnosticMessage?.Invoke(this, "Megas capability core stopped.");
        };

        if (!_process.Start())
        {
            throw new InvalidOperationException("Unable to start the embedded Megas capability core.");
        }

        try
        {
            // The private Python capability core is an owned implementation process,
            // not a separate product. Native Job Object containment guarantees that
            // it cannot survive the MegasMoves Windows runtime that owns it.
            _native.ContainProcess(_process.Id);
        }
        catch
        {
            try
            {
                if (!_process.HasExited)
                {
                    _process.Kill(entireProcessTree: true);
                    _process.WaitForExit(1000);
                }
            }
            catch
            {
            }
            _process.Dispose();
            _process = null;
            _pipeName = null;
            _activityPipeName = null;
            _logPipeName = null;
            _authToken = null;
            throw;
        }

        _process.BeginErrorReadLine();

        _pipe = new NamedPipeClientStream(
            ".",
            _pipeName,
            PipeDirection.InOut,
            PipeOptions.Asynchronous);

        using var connectTimeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        connectTimeout.CancelAfter(TimeSpan.FromSeconds(30));
        try
        {
            await _pipe.ConnectAsync(connectTimeout.Token);
        }
        catch
        {
            await ResetConnectionWithoutLockAsync();
            throw;
        }

        if (!_pipe.IsConnected)
        {
            await ResetConnectionWithoutLockAsync();
            throw new InvalidOperationException("Unable to connect to the authenticated Megas local pipe.");
        }
    }

    private static async Task WriteFrameAsync(
        NamedPipeClientStream pipe,
        byte[] payload,
        CancellationToken cancellationToken)
    {
        if (payload.Length <= 0 || payload.Length > MaxFrameBytes)
        {
            throw new InvalidOperationException("Megas IPC payload length is invalid.");
        }
        var header = new byte[4];
        BinaryPrimitives.WriteUInt32LittleEndian(header, checked((uint)payload.Length));
        await pipe.WriteAsync(header, cancellationToken);
        await pipe.WriteAsync(payload, cancellationToken);
        await pipe.FlushAsync(cancellationToken);
    }

    private static async Task<byte[]> ReadFrameAsync(
        NamedPipeClientStream pipe,
        CancellationToken cancellationToken)
    {
        var header = new byte[4];
        await ReadExactlyAsync(pipe, header, cancellationToken);
        var length = BinaryPrimitives.ReadUInt32LittleEndian(header);
        if (length == 0 || length > MaxFrameBytes)
        {
            throw new IOException("Megas IPC response length is invalid.");
        }
        var payload = new byte[checked((int)length)];
        await ReadExactlyAsync(pipe, payload, cancellationToken);
        return payload;
    }

    private static async Task ReadExactlyAsync(
        Stream stream,
        byte[] buffer,
        CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer.AsMemory(offset), cancellationToken);
            if (read == 0)
            {
                throw new IOException("Megas capability core closed the local IPC channel unexpectedly.");
            }
            offset += read;
        }
    }

    private async Task ResetConnectionWithoutLockAsync()
    {
        if (_pipe is not null)
        {
            try
            {
                await _pipe.DisposeAsync();
            }
            catch
            {
            }
            _pipe = null;
        }

        if (_process is not null)
        {
            try
            {
                if (!_process.HasExited && !_process.WaitForExit(750))
                {
                    _process.Kill(entireProcessTree: true);
                    _process.WaitForExit(1000);
                }
            }
            catch
            {
            }
            _process.Dispose();
            _process = null;
        }

        _pipeName = null;
        _activityPipeName = null;
        _logPipeName = null;
        _authToken = null;
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _shutdown.Cancel();

        await _gate.WaitAsync();
        try
        {
            await ResetConnectionWithoutLockAsync();
        }
        finally
        {
            _gate.Release();
        }

        await _activitySubscriptionGate.WaitAsync();
        _activitySubscriptionGate.Release();
        await _logSubscriptionGate.WaitAsync();
        _logSubscriptionGate.Release();

        _activitySubscriptionGate.Dispose();
        _logSubscriptionGate.Dispose();
        _gate.Dispose();
        _shutdown.Dispose();
    }
}
