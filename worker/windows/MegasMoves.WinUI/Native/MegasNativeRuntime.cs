using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Channels;

namespace MegasMoves.Native;

internal static partial class MegasNativeMethods
{
    internal const uint AbiVersion = 1;

    static MegasNativeMethods()
    {
        NativeLibrary.SetDllImportResolver(typeof(MegasNativeMethods).Assembly, ResolveNativeLibrary);
    }

    private static nint ResolveNativeLibrary(string libraryName, System.Reflection.Assembly assembly, DllImportSearchPath? searchPath)
    {
        if (libraryName is "megas_native" or "megas_native.dll")
        {
            var resolved = Services.RuntimePayloadResolver.ResolveNativeLibrary();
            if (!string.IsNullOrEmpty(resolved) && File.Exists(resolved) && NativeLibrary.TryLoad(resolved, out var resolvedHandle))
            {
                return resolvedHandle;
            }

            var candidates = new[]
            {
                Path.Combine(AppContext.BaseDirectory, "Runtime", "megas_native.dll"),
                Path.Combine(AppContext.BaseDirectory, "megas_native.dll"),
                Path.Combine(Environment.CurrentDirectory, "Runtime", "megas_native.dll"),
                Path.Combine(Environment.CurrentDirectory, "megas_native.dll"),
            };

            foreach (var candidate in candidates)
            {
                if (File.Exists(candidate) && NativeLibrary.TryLoad(candidate, out var handle))
                {
                    return handle;
                }
            }
        }

        return 0;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct NativeCallbacks
    {
        public uint StructSize;
        public uint AbiVersion;
        public nint OnEvent;
        public nint User;
    }

    [LibraryImport("megas_native", EntryPoint = "megas_runtime_abi_version")]
    internal static partial uint RuntimeAbiVersion();

    [LibraryImport("megas_native", EntryPoint = "megas_runtime_create")]
    internal static partial int RuntimeCreate(in NativeCallbacks callbacks, out nint handle);

    [LibraryImport("megas_native", EntryPoint = "megas_runtime_start")]
    internal static partial int RuntimeStart(nint handle);

    [LibraryImport("megas_native", EntryPoint = "megas_runtime_stop")]
    internal static partial int RuntimeStop(nint handle);

    [LibraryImport("megas_native", EntryPoint = "megas_runtime_contain_process")]
    internal static partial int RuntimeContainProcess(nint handle, uint processId);

    [LibraryImport("megas_native", EntryPoint = "megas_runtime_destroy")]
    internal static partial void RuntimeDestroy(nint handle);
}

public sealed class MegasNativeRuntime : IDisposable
{
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate void NativeEventCallback(nint utf8Json, nuint length, nint user);

    private readonly Channel<string> _events = Channel.CreateBounded<string>(
        new BoundedChannelOptions(1024)
        {
            FullMode = BoundedChannelFullMode.DropOldest,
            SingleReader = false,
            SingleWriter = true
        });

    private readonly NativeEventCallback _callback;
    private nint _handle;
    private bool _started;
    private bool _disposed;

    public MegasNativeRuntime()
    {
        _callback = OnNativeEvent;
    }

    public bool IsStarted => _started;

    public IAsyncEnumerable<string> Events(CancellationToken cancellationToken = default) =>
        _events.Reader.ReadAllAsync(cancellationToken);

    public void Start()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (_started)
        {
            return;
        }

        var actualAbi = MegasNativeMethods.RuntimeAbiVersion();
        if (actualAbi != MegasNativeMethods.AbiVersion)
        {
            throw new InvalidOperationException($"Megas native ABI mismatch: UI expects {MegasNativeMethods.AbiVersion}, DLL reports {actualAbi}.");
        }

        var callbacks = new MegasNativeMethods.NativeCallbacks
        {
            StructSize = checked((uint)Marshal.SizeOf<MegasNativeMethods.NativeCallbacks>()),
            AbiVersion = MegasNativeMethods.AbiVersion,
            OnEvent = Marshal.GetFunctionPointerForDelegate(_callback),
            User = 0
        };

        var create = MegasNativeMethods.RuntimeCreate(in callbacks, out _handle);
        if (create != 0 || _handle == 0)
        {
            _handle = 0;
            throw new InvalidOperationException($"Unable to create Megas native runtime (error {create}).");
        }

        var start = MegasNativeMethods.RuntimeStart(_handle);
        if (start != 0)
        {
            MegasNativeMethods.RuntimeDestroy(_handle);
            _handle = 0;
            throw new InvalidOperationException($"Unable to start Megas native runtime (error {start}).");
        }
        _started = true;
    }

    public void ContainProcess(int processId)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (!_started || _handle == 0)
        {
            throw new InvalidOperationException("Megas native runtime must be started before containing an owned process.");
        }
        if (processId <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(processId));
        }

        var result = MegasNativeMethods.RuntimeContainProcess(_handle, checked((uint)processId));
        if (result != 0)
        {
            throw new InvalidOperationException($"Unable to contain owned process {processId} in the Megas Windows Job Object (error {result}).");
        }
    }

    public void Stop()
    {
        if (!_started || _handle == 0)
        {
            return;
        }
        var result = MegasNativeMethods.RuntimeStop(_handle);
        if (result != 0)
        {
            throw new InvalidOperationException($"Unable to stop Megas native runtime (error {result}).");
        }
        _started = false;
    }

    private void OnNativeEvent(nint utf8Json, nuint length, nint user)
    {
        if (utf8Json == 0 || length == 0 || length > 1024 * 1024)
        {
            return;
        }

        try
        {
            var byteLength = checked((int)length);
            var bytes = new byte[byteLength];
            Marshal.Copy(utf8Json, bytes, 0, byteLength);
            _events.Writer.TryWrite(Encoding.UTF8.GetString(bytes));
        }
        catch
        {
            // Native callback boundaries must never throw across the C ABI.
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;

        if (_handle != 0)
        {
            try
            {
                if (_started)
                {
                    MegasNativeMethods.RuntimeStop(_handle);
                }
            }
            finally
            {
                MegasNativeMethods.RuntimeDestroy(_handle);
                _handle = 0;
                _started = false;
            }
        }
        _events.Writer.TryComplete();
        GC.KeepAlive(_callback);
    }
}
