using Microsoft.Extensions.Hosting;
using MegasMoves.Native;

namespace MegasMoves.Services;

public sealed class NativeRuntimeHostedService : IHostedService, IDisposable
{
    private readonly MegasNativeRuntime _native;
    private readonly MegasCoreBridge _bridge;
    private readonly CancellationTokenSource _shutdown = new();
    private Task? _eventPump;

    public NativeRuntimeHostedService(MegasNativeRuntime native, MegasCoreBridge bridge)
    {
        _native = native;
        _bridge = bridge;
    }

    public Task StartAsync(CancellationToken cancellationToken)
    {
        _native.Start();
        _eventPump = Task.Run(() => PumpNativeEventsAsync(_shutdown.Token), CancellationToken.None);
        return Task.CompletedTask;
    }

    private async Task PumpNativeEventsAsync(CancellationToken cancellationToken)
    {
        try
        {
            await foreach (var json in _native.Events(cancellationToken))
            {
                try
                {
                    await _bridge.RequestAsync(
                        "native.event.record",
                        new { json },
                        cancellationToken);
                }
                catch when (!cancellationToken.IsCancellationRequested)
                {
                    // Native evidence is best-effort at this boundary. The source
                    // event stays bounded in the native channel and must never
                    // crash or block the product UI if the Python core is down.
                }
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
    }

    public async Task StopAsync(CancellationToken cancellationToken)
    {
        _shutdown.Cancel();
        try
        {
            _native.Stop();
        }
        catch
        {
            // Shutdown continues; process teardown releases the native handle.
        }

        if (_eventPump is not null)
        {
            try
            {
                await _eventPump.WaitAsync(cancellationToken);
            }
            catch (OperationCanceledException)
            {
            }
        }
    }

    public void Dispose()
    {
        _shutdown.Cancel();
        _shutdown.Dispose();
    }
}
