using System.Diagnostics;
using MegasMoves.Native;

namespace MegasMoves.Services;

internal static class CoreStdioWorker
{
    internal static bool IsServeRequest(string[] args)
    {
        return args.Length == 2 &&
               string.Equals(args[1], "serve", StringComparison.OrdinalIgnoreCase);
    }

    internal static async Task<int?> RunIfRequestedAsync(string[] args)
    {
        if (!IsServeRequest(args))
        {
            return null;
        }

        var corePath = ResolveCorePath();
        if (corePath is null)
        {
            return 72;
        }

        var startInfo = new ProcessStartInfo
        {
            FileName = corePath,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = false,
            RedirectStandardOutput = false,
            RedirectStandardError = false,
            WorkingDirectory = AppContext.BaseDirectory,
        };
        startInfo.ArgumentList.Add("--stdio");

        var productExecutable = Environment.ProcessPath;
        if (!string.IsNullOrWhiteSpace(productExecutable))
        {
            startInfo.Environment["MEGAS_EXECUTABLE"] = productExecutable;
        }

        var browserRuntime = ResolveBrowserRuntime();
        if (browserRuntime is not null)
        {
            startInfo.Environment["MEGAS_BROWSER_RUNTIME_DIR"] = browserRuntime;
            startInfo.Environment["PLAYWRIGHT_BROWSERS_PATH"] = browserRuntime;
        }

        using var native = new MegasNativeRuntime();
        using var child = new Process { StartInfo = startInfo };
        try
        {
            native.Start();
            if (!child.Start())
            {
                return 73;
            }
            native.ContainProcess(child.Id);
            await child.WaitForExitAsync();
            return child.ExitCode;
        }
        catch
        {
            try
            {
                if (!child.HasExited)
                {
                    child.Kill(entireProcessTree: true);
                }
            }
            catch
            {
            }
            return 74;
        }
        finally
        {
            try
            {
                native.Stop();
            }
            catch
            {
            }
        }
    }

    private static string? ResolveCorePath()
    {
        var path = RuntimePayloadResolver.ResolveCoreExecutable();
        return File.Exists(path) ? path : null;
    }

    private static string? ResolveBrowserRuntime()
    {
        return RuntimePayloadResolver.ResolveBrowserRuntime();
    }
}
