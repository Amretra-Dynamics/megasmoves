using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;

namespace MegasMoves.Services;

public static class RuntimePayloadResolver
{
    private static readonly object _sync = new();
    private static string? _cachedExtractionRoot;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr GetModuleHandleW(string? lpModuleName);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetModuleFileNameW(IntPtr hModule, StringBuilder lpFilename, int nSize);

    public static string? ResolveExtractionRoot()
    {
        lock (_sync)
        {
            if (_cachedExtractionRoot is not null && Directory.Exists(_cachedExtractionRoot))
            {
                return _cachedExtractionRoot;
            }

            // 1. Check if megas_native.dll is loaded in the process and query its path
            try
            {
                var hModule = GetModuleHandleW("megas_native.dll");
                if (hModule == IntPtr.Zero)
                {
                    hModule = GetModuleHandleW("megas_native");
                }
                if (hModule != IntPtr.Zero)
                {
                    var sb = new StringBuilder(1024);
                    if (GetModuleFileNameW(hModule, sb, sb.Capacity) > 0)
                    {
                        var modulePath = sb.ToString();
                        var dir = Path.GetDirectoryName(modulePath);
                        if (!string.IsNullOrEmpty(dir))
                        {
                            if (string.Equals(Path.GetFileName(dir), "Runtime", StringComparison.OrdinalIgnoreCase))
                            {
                                var root = Path.GetDirectoryName(dir);
                                if (!string.IsNullOrEmpty(root) && Directory.Exists(root))
                                {
                                    _cachedExtractionRoot = root;
                                    return root;
                                }
                            }
                            else if (Directory.Exists(dir))
                            {
                                _cachedExtractionRoot = dir;
                                return dir;
                            }
                        }
                    }
                }
            }
            catch { }

            // 2. Scan %TEMP%\.net\MegasMoves for single-file bundle extractions
            try
            {
                var tempNet = Path.Combine(Path.GetTempPath(), ".net", "MegasMoves");
                if (Directory.Exists(tempNet))
                {
                    var best = new DirectoryInfo(tempNet)
                        .GetDirectories()
                        .OrderByDescending(d => d.LastWriteTimeUtc)
                        .FirstOrDefault(d =>
                            File.Exists(Path.Combine(d.FullName, "Runtime", "MegasMoves.Core.exe")) ||
                            File.Exists(Path.Combine(d.FullName, "MegasMoves.Core.exe")));
                    if (best is not null)
                    {
                        _cachedExtractionRoot = best.FullName;
                        return best.FullName;
                    }
                }
            }
            catch { }

            return null;
        }
    }

    public static IEnumerable<string> CandidateDirectories()
    {
        yield return AppContext.BaseDirectory;
        var extract = ResolveExtractionRoot();
        if (!string.IsNullOrEmpty(extract) && !string.Equals(extract, AppContext.BaseDirectory, StringComparison.OrdinalIgnoreCase))
        {
            yield return extract;
        }
        if (!string.IsNullOrEmpty(Environment.CurrentDirectory) &&
            !string.Equals(Environment.CurrentDirectory, AppContext.BaseDirectory, StringComparison.OrdinalIgnoreCase) &&
            !string.Equals(Environment.CurrentDirectory, extract, StringComparison.OrdinalIgnoreCase))
        {
            yield return Environment.CurrentDirectory;
        }
    }

    public static string ResolveCoreExecutable()
    {
        foreach (var dir in CandidateDirectories())
        {
            var p1 = Path.Combine(dir, "Runtime", "MegasMoves.Core.exe");
            if (File.Exists(p1)) return p1;
            var p2 = Path.Combine(dir, "MegasMoves.Core.exe");
            if (File.Exists(p2)) return p2;
        }

        return Path.Combine(AppContext.BaseDirectory, "Runtime", "MegasMoves.Core.exe");
    }

    public static string? ResolveNativeLibrary()
    {
        foreach (var dir in CandidateDirectories())
        {
            var p1 = Path.Combine(dir, "Runtime", "megas_native.dll");
            if (File.Exists(p1)) return p1;
            var p2 = Path.Combine(dir, "megas_native.dll");
            if (File.Exists(p2)) return p2;
        }
        return null;
    }

    public static string? ResolveBrowserRuntime()
    {
        foreach (var dir in CandidateDirectories())
        {
            var p1 = Path.Combine(dir, "Runtime", "browser");
            if (Directory.Exists(p1)) return p1;
            var p2 = Path.Combine(dir, "browser");
            if (Directory.Exists(p2)) return p2;
        }
        return null;
    }

    public static string? ResolveAsset(string relativePath)
    {
        foreach (var dir in CandidateDirectories())
        {
            var p1 = Path.Combine(dir, relativePath);
            if (File.Exists(p1)) return p1;
            var p2 = Path.Combine(dir, "Assets", relativePath);
            if (File.Exists(p2)) return p2;
        }
        return null;
    }

    public static string? ResolveRuntimeFile(string relativePath)
    {
        foreach (var dir in CandidateDirectories())
        {
            var p1 = Path.Combine(dir, "Runtime", relativePath);
            if (File.Exists(p1)) return p1;
            var p2 = Path.Combine(dir, relativePath);
            if (File.Exists(p2)) return p2;
        }
        return null;
    }
}
