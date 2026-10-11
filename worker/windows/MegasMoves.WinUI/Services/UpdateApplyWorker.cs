using System.Diagnostics;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace MegasMoves.Services;

internal sealed record UpdateApplyPlan(
    int SchemaVersion,
    string Version,
    string TargetPath,
    string StagedPath,
    string RollbackPath,
    string StagedSha256,
    string RollbackSha256,
    string StatusPath,
    string PlanMac);

public sealed class UpdateApplyLauncher
{
    private const int PlanKeyBytes = 32;
    private readonly UpdateCoordinator _updates;

    public UpdateApplyLauncher(UpdateCoordinator updates)
    {
        _updates = updates;
    }

    public async Task<string> LaunchAsync(
        StagedUpdate staged,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(staged);
        var preflight = await _updates.PreflightAsync(cancellationToken);
        if (!preflight.Safe)
        {
            throw new InvalidOperationException($"Update apply is blocked: {preflight.Summary}");
        }

        var current = Environment.ProcessPath;
        if (string.IsNullOrWhiteSpace(current) || !File.Exists(current))
        {
            throw new InvalidOperationException("Current MegasMoves.exe path is unavailable.");
        }
        if (!File.Exists(staged.StagedArtifactPath) || !File.Exists(staged.RollbackArtifactPath))
        {
            throw new FileNotFoundException("Staged update or rollback artifact is unavailable.");
        }

        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(current);
        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(staged.StagedArtifactPath);
        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(staged.RollbackArtifactPath);

        var rollbackSha = await UpdateApplyWorker.Sha256FileAsync(staged.RollbackArtifactPath, cancellationToken);
        var currentSha = await UpdateApplyWorker.Sha256FileAsync(current, cancellationToken);
        if (!FixedHexEquals(currentSha, rollbackSha))
        {
            throw new InvalidDataException("Rollback snapshot no longer matches the running MegasMoves.exe.");
        }
        var stagedSha = await UpdateApplyWorker.Sha256FileAsync(staged.StagedArtifactPath, cancellationToken);
        if (!FixedHexEquals(stagedSha, staged.Manifest.Sha256))
        {
            throw new InvalidDataException("Staged MegasMoves.exe no longer matches signed metadata.");
        }

        var stageDirectory = Path.GetDirectoryName(staged.StagedArtifactPath)
            ?? throw new InvalidDataException("Staged update path has no parent directory.");
        var planPath = Path.Combine(stageDirectory, "apply-plan.json");
        var statusPath = Path.Combine(stageDirectory, "apply-status.json");
        var unsignedPlan = new UpdateApplyPlan(
            1,
            staged.Manifest.Version,
            Path.GetFullPath(current),
            Path.GetFullPath(staged.StagedArtifactPath),
            Path.GetFullPath(staged.RollbackArtifactPath),
            stagedSha,
            rollbackSha,
            Path.GetFullPath(statusPath),
            string.Empty);

        var planKey = RandomNumberGenerator.GetBytes(PlanKeyBytes);
        try
        {
            var plan = unsignedPlan with
            {
                PlanMac = UpdateApplyWorker.ComputePlanMac(unsignedPlan, planKey),
            };
            WriteJsonAtomic(planPath, plan);

            using var keyPipe = new AnonymousPipeServerStream(
                PipeDirection.Out,
                HandleInheritability.Inheritable);
            var startInfo = new ProcessStartInfo
            {
                FileName = staged.StagedArtifactPath,
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = stageDirectory,
            };
            startInfo.ArgumentList.Add("--megas-apply-update");
            startInfo.ArgumentList.Add(planPath);
            startInfo.ArgumentList.Add("--parent-pid");
            startInfo.ArgumentList.Add(Environment.ProcessId.ToString(System.Globalization.CultureInfo.InvariantCulture));
            startInfo.ArgumentList.Add("--plan-key-handle");
            startInfo.ArgumentList.Add(keyPipe.GetClientHandleAsString());

            var helper = Process.Start(startInfo)
                ?? throw new InvalidOperationException("Unable to start the staged MegasMoves update worker.");
            keyPipe.DisposeLocalCopyOfClientHandle();
            try
            {
                await keyPipe.WriteAsync(planKey, cancellationToken);
                await keyPipe.FlushAsync(cancellationToken);
            }
            catch
            {
                try
                {
                    if (!helper.HasExited)
                    {
                        helper.Kill(entireProcessTree: true);
                    }
                }
                catch
                {
                }
                throw;
            }
            finally
            {
                helper.Dispose();
            }
            return planPath;
        }
        finally
        {
            CryptographicOperations.ZeroMemory(planKey);
        }
    }

    private static bool FixedHexEquals(string left, string right) =>
        left.Length == right.Length &&
        CryptographicOperations.FixedTimeEquals(
            Encoding.ASCII.GetBytes(left.ToLowerInvariant()),
            Encoding.ASCII.GetBytes(right.ToLowerInvariant()));

    private static void WriteJsonAtomic<T>(string path, T value)
    {
        var temporary = path + ".tmp";
        var json = JsonSerializer.Serialize(value, UpdateApplyWorker.JsonOptions) + Environment.NewLine;
        File.WriteAllText(temporary, json, new UTF8Encoding(false));
        File.Move(temporary, path, overwrite: true);
    }
}

public static class UpdateApplyWorker
{
    internal static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = false,
    };

    private const int MaxPlanBytes = 64 * 1024;
    private const int PlanKeyBytes = 32;
    private const int ReadyTokenBytes = 32;
    private static readonly TimeSpan ParentExitTimeout = TimeSpan.FromSeconds(30);
    private static readonly TimeSpan NewBuildReadyTimeout = TimeSpan.FromSeconds(30);

    public static async Task<int?> RunIfRequestedAsync(
        string[] commandLine,
        CancellationToken cancellationToken = default)
    {
        var mode = Array.IndexOf(commandLine, "--megas-apply-update");
        if (mode < 0)
        {
            return null;
        }
        if (mode + 1 >= commandLine.Length)
        {
            return 64;
        }
        var parentFlag = Array.IndexOf(commandLine, "--parent-pid");
        if (parentFlag < 0 || parentFlag + 1 >= commandLine.Length ||
            !int.TryParse(commandLine[parentFlag + 1], out var parentPid) || parentPid <= 0)
        {
            return 64;
        }
        var keyFlag = Array.IndexOf(commandLine, "--plan-key-handle");
        if (keyFlag < 0 || keyFlag + 1 >= commandLine.Length ||
            string.IsNullOrWhiteSpace(commandLine[keyFlag + 1]))
        {
            return 64;
        }

        byte[]? planKey = null;
        try
        {
            planKey = await ReadPlanKeyAsync(commandLine[keyFlag + 1], cancellationToken);
            return await ApplyAsync(commandLine[mode + 1], parentPid, planKey, cancellationToken);
        }
        catch
        {
            // Update helper errors are persisted to the bounded status record when
            // possible. Never emit paths, credentials or raw exceptions to a console.
            return 1;
        }
        finally
        {
            if (planKey is not null)
            {
                CryptographicOperations.ZeroMemory(planKey);
            }
        }
    }

    public static async Task SignalReadyIfRequestedAsync(CancellationToken cancellationToken = default)
    {
        var pipeName = Environment.GetEnvironmentVariable("MEGAS_UPDATE_READY_PIPE")?.Trim();
        var tokenText = Environment.GetEnvironmentVariable("MEGAS_UPDATE_READY_TOKEN")?.Trim();
        Environment.SetEnvironmentVariable("MEGAS_UPDATE_READY_PIPE", null);
        Environment.SetEnvironmentVariable("MEGAS_UPDATE_READY_TOKEN", null);
        Environment.SetEnvironmentVariable("MEGAS_UPDATE_PLAN", null);
        if (string.IsNullOrWhiteSpace(pipeName) || string.IsNullOrWhiteSpace(tokenText))
        {
            return;
        }

        byte[] token;
        try
        {
            token = Convert.FromBase64String(tokenText);
        }
        catch (FormatException)
        {
            return;
        }
        if (token.Length != ReadyTokenBytes)
        {
            CryptographicOperations.ZeroMemory(token);
            return;
        }

        try
        {
            await using var pipe = new NamedPipeClientStream(
                ".",
                pipeName,
                PipeDirection.Out,
                PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(TimeSpan.FromSeconds(5));
            try
            {
                await pipe.ConnectAsync(timeout.Token);
                await pipe.WriteAsync(token, timeout.Token);
                await pipe.FlushAsync(timeout.Token);
            }
            catch (OperationCanceledException)
            {
            }
            catch (IOException)
            {
            }
        }
        finally
        {
            CryptographicOperations.ZeroMemory(token);
        }
    }

    internal static async Task<string> Sha256FileAsync(
        string path,
        CancellationToken cancellationToken = default)
    {
        await using var stream = new FileStream(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            128 * 1024,
            FileOptions.Asynchronous | FileOptions.SequentialScan);
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var buffer = new byte[128 * 1024];
        while (true)
        {
            var read = await stream.ReadAsync(buffer.AsMemory(), cancellationToken);
            if (read == 0)
            {
                break;
            }
            hash.AppendData(buffer, 0, read);
        }
        return Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
    }

    internal static string ComputePlanMac(UpdateApplyPlan plan, ReadOnlySpan<byte> key)
    {
        if (key.Length != PlanKeyBytes)
        {
            throw new ArgumentException("Update plan authentication key must be 32 bytes.", nameof(key));
        }
        var payload = PlanAuthenticationBytes(plan);
        using var hmac = new HMACSHA256(key.ToArray());
        return Convert.ToHexString(hmac.ComputeHash(payload)).ToLowerInvariant();
    }

    private static byte[] PlanAuthenticationBytes(UpdateApplyPlan plan) => Encoding.UTF8.GetBytes(string.Join(
        "\n",
        new[]
        {
            plan.SchemaVersion.ToString(System.Globalization.CultureInfo.InvariantCulture),
            plan.Version,
            plan.TargetPath,
            plan.StagedPath,
            plan.RollbackPath,
            plan.StagedSha256,
            plan.RollbackSha256,
            plan.StatusPath,
            string.Empty,
        }));

    private static async Task<byte[]> ReadPlanKeyAsync(
        string inheritedHandle,
        CancellationToken cancellationToken)
    {
        await using var pipe = new AnonymousPipeClientStream(PipeDirection.In, inheritedHandle);
        var key = new byte[PlanKeyBytes];
        var offset = 0;
        while (offset < key.Length)
        {
            var read = await pipe.ReadAsync(key.AsMemory(offset), cancellationToken);
            if (read <= 0)
            {
                CryptographicOperations.ZeroMemory(key);
                throw new EndOfStreamException("Update plan authentication key handoff ended early.");
            }
            offset += read;
        }
        return key;
    }

    private static async Task<int> ApplyAsync(
        string planPath,
        int parentPid,
        byte[] planKey,
        CancellationToken cancellationToken)
    {
        var plan = await ReadAndValidatePlanAsync(planPath, planKey, cancellationToken);
        await WaitForParentExitAsync(parentPid, cancellationToken);

        var targetSha = await Sha256FileAsync(plan.TargetPath, cancellationToken);
        if (!FixedHexEquals(targetSha, plan.RollbackSha256))
        {
            WriteStatus(plan, "failed", "target-changed-before-apply");
            return 2;
        }
        var stagedSha = await Sha256FileAsync(plan.StagedPath, cancellationToken);
        if (!FixedHexEquals(stagedSha, plan.StagedSha256))
        {
            WriteStatus(plan, "failed", "staged-hash-mismatch");
            Relaunch(plan.TargetPath);
            return 3;
        }
        var rollbackSha = await Sha256FileAsync(plan.RollbackPath, cancellationToken);
        if (!FixedHexEquals(rollbackSha, plan.RollbackSha256))
        {
            WriteStatus(plan, "failed", "rollback-hash-mismatch");
            Relaunch(plan.TargetPath);
            return 4;
        }

        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(plan.TargetPath);
        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(plan.StagedPath);
        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(plan.RollbackPath);

        var targetDirectory = Path.GetDirectoryName(plan.TargetPath)
            ?? throw new InvalidDataException("Update target has no parent directory.");
        var replacement = Path.Combine(targetDirectory, $".MegasMoves.update-{Guid.NewGuid():N}.tmp");
        try
        {
            File.Copy(plan.StagedPath, replacement, overwrite: false);
            WindowsPeVerifier.ValidateMegasMovesAmd64Gui(replacement);
            var replacementSha = await Sha256FileAsync(replacement, cancellationToken);
            if (!FixedHexEquals(replacementSha, plan.StagedSha256))
            {
                throw new InvalidDataException("Copied update failed hash verification.");
            }
            File.Replace(replacement, plan.TargetPath, destinationBackupFileName: null, ignoreMetadataErrors: true);
            replacement = string.Empty;
        }
        catch
        {
            TryDelete(replacement);
            WriteStatus(plan, "failed", "replace-failed");
            Relaunch(plan.TargetPath);
            return 5;
        }

        var readyPipeName = $"megasmoves-update-{Environment.ProcessId}-{Guid.NewGuid():N}";
        var readyToken = RandomNumberGenerator.GetBytes(ReadyTokenBytes);
        var readyTokenText = Convert.ToBase64String(readyToken);

        try
        {
            using var readyServer = new NamedPipeServerStream(
                readyPipeName,
                PipeDirection.In,
                1,
                PipeTransmissionMode.Byte,
                PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly,
                4096,
                4096);
            var newProcess = Relaunch(
                plan.TargetPath,
                new Dictionary<string, string>
                {
                    ["MEGAS_UPDATE_READY_PIPE"] = readyPipeName,
                    ["MEGAS_UPDATE_READY_TOKEN"] = readyTokenText,
                    ["MEGAS_UPDATE_PLAN"] = Path.GetFileName(plan.StatusPath),
                });
            if (newProcess is null)
            {
                await RollbackAsync(plan, null, cancellationToken);
                return 6;
            }

            try
            {
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                timeout.CancelAfter(NewBuildReadyTimeout);
                var connected = readyServer.WaitForConnectionAsync(timeout.Token);
                var exited = newProcess.WaitForExitAsync(timeout.Token);
                var completed = await Task.WhenAny(connected, exited);
                if (completed != connected)
                {
                    await RollbackAsync(plan, newProcess, cancellationToken);
                    return 7;
                }
                await connected;

                var received = new byte[ReadyTokenBytes];
                var offset = 0;
                while (offset < received.Length)
                {
                    var read = await readyServer.ReadAsync(received.AsMemory(offset), timeout.Token);
                    if (read <= 0)
                    {
                        break;
                    }
                    offset += read;
                }
                if (offset != received.Length || !CryptographicOperations.FixedTimeEquals(received, readyToken))
                {
                    CryptographicOperations.ZeroMemory(received);
                    await RollbackAsync(plan, newProcess, cancellationToken);
                    return 8;
                }
                CryptographicOperations.ZeroMemory(received);

                WriteStatus(plan, "applied", "authoritative-startup-confirmed");
                return 0;
            }
            catch (OperationCanceledException)
            {
                await RollbackAsync(plan, newProcess, CancellationToken.None);
                return 9;
            }
            finally
            {
                newProcess.Dispose();
            }
        }
        finally
        {
            CryptographicOperations.ZeroMemory(readyToken);
        }
    }

    private static async Task RollbackAsync(
        UpdateApplyPlan plan,
        Process? failedProcess,
        CancellationToken cancellationToken)
    {
        if (failedProcess is { HasExited: false })
        {
            try
            {
                failedProcess.Kill(entireProcessTree: true);
                await failedProcess.WaitForExitAsync(cancellationToken);
            }
            catch
            {
            }
        }

        var targetDirectory = Path.GetDirectoryName(plan.TargetPath)!;
        var replacement = Path.Combine(targetDirectory, $".MegasMoves.rollback-{Guid.NewGuid():N}.tmp");
        try
        {
            File.Copy(plan.RollbackPath, replacement, overwrite: false);
            WindowsPeVerifier.ValidateMegasMovesAmd64Gui(replacement);
            var sha = await Sha256FileAsync(replacement, cancellationToken);
            if (!FixedHexEquals(sha, plan.RollbackSha256))
            {
                throw new InvalidDataException("Rollback copy failed hash verification.");
            }
            File.Replace(replacement, plan.TargetPath, destinationBackupFileName: null, ignoreMetadataErrors: true);
            replacement = string.Empty;
            WriteStatus(plan, "rolled-back", "new-build-readiness-failed");
            Relaunch(plan.TargetPath);
        }
        catch
        {
            TryDelete(replacement);
            WriteStatus(plan, "rollback-failed", "manual-recovery-required");
        }
    }

    private static async Task<UpdateApplyPlan> ReadAndValidatePlanAsync(
        string planPath,
        byte[] planKey,
        CancellationToken cancellationToken)
    {
        var root = UpdateRoot();
        var fullPlan = EnsureUnder(Path.Combine(root, "staging"), planPath);
        var info = new FileInfo(fullPlan);
        if (!info.Exists || info.Length <= 0 || info.Length > MaxPlanBytes)
        {
            throw new InvalidDataException("Update apply plan is missing or exceeds the bound.");
        }

        var raw = await File.ReadAllTextAsync(fullPlan, cancellationToken);
        using (var document = JsonDocument.Parse(raw, new JsonDocumentOptions
        {
            AllowTrailingCommas = false,
            CommentHandling = JsonCommentHandling.Disallow,
            MaxDepth = 4,
        }))
        {
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                throw new InvalidDataException("Update apply plan must be an object.");
            }
            var allowed = new HashSet<string>(StringComparer.Ordinal)
            {
                "schemaVersion",
                "version",
                "targetPath",
                "stagedPath",
                "rollbackPath",
                "stagedSha256",
                "rollbackSha256",
                "statusPath",
                "planMac",
            };
            foreach (var property in document.RootElement.EnumerateObject())
            {
                if (!allowed.Contains(property.Name))
                {
                    throw new InvalidDataException($"Update apply plan contains unsupported property '{property.Name}'.");
                }
            }
        }

        var plan = JsonSerializer.Deserialize<UpdateApplyPlan>(raw, JsonOptions)
            ?? throw new InvalidDataException("Update apply plan is invalid.");
        if (plan.SchemaVersion != 1)
        {
            throw new InvalidDataException("Unsupported update apply plan schema.");
        }
        if (!IsSha256(plan.PlanMac))
        {
            throw new InvalidDataException("Update apply plan authentication code is invalid.");
        }
        var expectedMac = ComputePlanMac(plan with { PlanMac = string.Empty }, planKey);
        if (!FixedHexEquals(expectedMac, plan.PlanMac))
        {
            throw new CryptographicException("Update apply plan authentication failed.");
        }

        var processPath = Path.GetFullPath(Environment.ProcessPath
            ?? throw new InvalidOperationException("Update worker executable path is unavailable."));
        var staged = EnsureUnder(Path.Combine(root, "staging"), plan.StagedPath);
        var rollback = EnsureUnder(Path.Combine(root, "rollback"), plan.RollbackPath);
        var status = EnsureUnder(Path.Combine(root, "staging"), plan.StatusPath);
        var target = Path.GetFullPath(plan.TargetPath);

        if (!string.Equals(processPath, staged, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("Update worker is not running from the staged MegasMoves executable.");
        }
        if (!string.Equals(Path.GetFileName(target), "MegasMoves.exe", StringComparison.Ordinal))
        {
            throw new InvalidDataException("Update target must be MegasMoves.exe.");
        }
        if (!string.Equals(Path.GetFileName(staged), "MegasMoves.exe", StringComparison.Ordinal) ||
            !string.Equals(Path.GetFileName(rollback), "MegasMoves.exe", StringComparison.Ordinal))
        {
            throw new InvalidDataException("Update and rollback artifacts must be MegasMoves.exe.");
        }
        if (!IsSha256(plan.StagedSha256) || !IsSha256(plan.RollbackSha256))
        {
            throw new InvalidDataException("Update apply plan contains an invalid SHA-256.");
        }
        if (string.IsNullOrWhiteSpace(plan.Version) || plan.Version.Length > 64)
        {
            throw new InvalidDataException("Update apply plan contains an invalid version.");
        }

        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(staged);
        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(rollback);

        return plan with
        {
            TargetPath = target,
            StagedPath = staged,
            RollbackPath = rollback,
            StatusPath = status,
        };
    }

    private static async Task WaitForParentExitAsync(int parentPid, CancellationToken cancellationToken)
    {
        Process? parent = null;
        try
        {
            parent = Process.GetProcessById(parentPid);
        }
        catch (ArgumentException)
        {
            return;
        }

        using (parent)
        using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
        {
            timeout.CancelAfter(ParentExitTimeout);
            await parent.WaitForExitAsync(timeout.Token);
        }
    }

    private static Process? Relaunch(
        string executable,
        IReadOnlyDictionary<string, string>? environment = null)
    {
        try
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = executable,
                UseShellExecute = false,
                WorkingDirectory = Path.GetDirectoryName(executable) ?? AppContext.BaseDirectory,
            };
            if (environment is not null)
            {
                foreach (var item in environment)
                {
                    startInfo.Environment[item.Key] = item.Value;
                }
            }
            return Process.Start(startInfo);
        }
        catch
        {
            return null;
        }
    }

    private static void WriteStatus(UpdateApplyPlan plan, string state, string reason)
    {
        try
        {
            var payload = JsonSerializer.Serialize(new
            {
                schema_version = 1,
                version = plan.Version,
                state,
                reason,
                at = DateTimeOffset.UtcNow.ToString("O"),
            }, JsonOptions) + Environment.NewLine;
            var temporary = plan.StatusPath + ".tmp";
            File.WriteAllText(temporary, payload, new UTF8Encoding(false));
            File.Move(temporary, plan.StatusPath, overwrite: true);
        }
        catch
        {
        }
    }

    private static string UpdateRoot() => Path.GetFullPath(Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Amretra Dynamics",
        "MegasMoves",
        "updates"));

    private static string EnsureUnder(string root, string candidate)
    {
        var normalizedRoot = Path.GetFullPath(root);
        var full = Path.GetFullPath(candidate);
        if (!full.StartsWith(normalizedRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("Update path escaped the managed update root.");
        }
        return full;
    }

    private static bool IsSha256(string value) =>
        value.Length == 64 && value.All(Uri.IsHexDigit);

    private static bool FixedHexEquals(string left, string right) =>
        left.Length == right.Length &&
        CryptographicOperations.FixedTimeEquals(
            Encoding.ASCII.GetBytes(left.ToLowerInvariant()),
            Encoding.ASCII.GetBytes(right.ToLowerInvariant()));

    private static void TryDelete(string path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return;
        }
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch
        {
        }
    }
}
