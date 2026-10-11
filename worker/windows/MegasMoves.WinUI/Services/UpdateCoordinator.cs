using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace MegasMoves.Services;

public enum UpdateChannel
{
    Stable,
    Internal,
    Dev,
}

public sealed record UpdatePolicy(UpdateChannel Channel, bool AutomaticChecksEnabled);

public sealed record UpdateBlocker(string Code, int Count, string Message);

public sealed record UpdatePreflight(bool Safe, IReadOnlyList<UpdateBlocker> Blockers)
{
    public string Summary => Safe
        ? "Safe to stage an update. Installation still requires explicit user action."
        : string.Join(" ", Blockers.Select(item => item.Message));
}

public sealed record VerifiedUpdateManifest(
    int SchemaVersion,
    string Version,
    UpdateChannel Channel,
    string Artifact,
    long SizeBytes,
    string Sha256,
    Uri DownloadUri,
    string SignatureAlgorithm,
    byte[] Signature,
    byte[] SignedPayload);

public sealed record StagedUpdate(
    VerifiedUpdateManifest Manifest,
    string StagedArtifactPath,
    string RollbackArtifactPath,
    string RollbackMetadataPath);

/// <summary>
/// Coordinates Windows update policy, trust verification and staging only.
///
/// The running product is never replaced from this class. Applying a staged
/// update requires an explicit one-shot handoff so rollback and process shutdown
/// can be coordinated safely. Automatic installation is not a supported policy.
/// </summary>
public sealed class UpdateCoordinator
{
    public const int ManifestSchemaVersion = 1;
    public const long MaxUpdateBytes = 512L * 1024 * 1024;
    public const int MaxManifestChars = 64 * 1024;
    public const string SignatureAlgorithm = "ECDSA_P256_SHA256_DER";

    private static readonly HttpClient Http = new(new SocketsHttpHandler
    {
        AllowAutoRedirect = false,
        AutomaticDecompression = System.Net.DecompressionMethods.None,
        ConnectTimeout = TimeSpan.FromSeconds(10),
    })
    {
        Timeout = TimeSpan.FromMinutes(5),
    };

    private readonly UiStateStore _state;
    private readonly MegasCoreBridge _bridge;
    private readonly string _updatesRoot;

    public UpdateCoordinator(UiStateStore state, MegasCoreBridge bridge)
    {
        _state = state;
        _bridge = bridge;
        _updatesRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Amretra Dynamics",
            "MegasMoves",
            "updates");
    }

    public UpdatePolicy Policy
    {
        get
        {
            var channel = ParseChannel(_state.GetString("updates.channel"));
            var automaticChecks = string.Equals(
                _state.GetString("updates.automatic-checks"),
                "true",
                StringComparison.OrdinalIgnoreCase);
            return new UpdatePolicy(channel, automaticChecks);
        }
    }

    public void SetPolicy(UpdateChannel channel, bool automaticChecksEnabled)
    {
        _state.SetString("updates.channel", ChannelName(channel));
        _state.SetString("updates.automatic-checks", automaticChecksEnabled ? "true" : "false");
    }

    public async Task<UpdatePreflight> PreflightAsync(CancellationToken cancellationToken = default)
    {
        var result = await _bridge.RequestAsync("update.preflight", new { }, cancellationToken);
        var blockers = new List<UpdateBlocker>();
        if (result.TryGetProperty("blockers", out var rows) && rows.ValueKind == JsonValueKind.Array)
        {
            foreach (var row in rows.EnumerateArray())
            {
                blockers.Add(new UpdateBlocker(
                    Text(row, "code"),
                    Int(row, "count"),
                    Text(row, "message")));
            }
        }
        return new UpdatePreflight(
            result.TryGetProperty("safe", out var safe) && safe.ValueKind == JsonValueKind.True,
            blockers);
    }

    public VerifiedUpdateManifest VerifyManifest(string json)
    {
        if (string.IsNullOrWhiteSpace(json) || json.Length > MaxManifestChars)
        {
            throw new InvalidDataException("Update manifest is empty or exceeds the 64 KiB bound.");
        }

        using var document = JsonDocument.Parse(json, new JsonDocumentOptions
        {
            AllowTrailingCommas = false,
            CommentHandling = JsonCommentHandling.Disallow,
            MaxDepth = 8,
        });
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException("Update manifest must be a JSON object.");
        }

        var allowed = new HashSet<string>(StringComparer.Ordinal)
        {
            "schema_version",
            "version",
            "channel",
            "artifact",
            "size_bytes",
            "sha256",
            "url",
            "signature_algorithm",
            "signature",
        };
        foreach (var property in root.EnumerateObject())
        {
            if (!allowed.Contains(property.Name))
            {
                throw new InvalidDataException($"Update manifest contains unsupported property '{property.Name}'.");
            }
        }

        var schemaVersion = RequiredInt(root, "schema_version");
        if (schemaVersion != ManifestSchemaVersion)
        {
            throw new InvalidDataException($"Unsupported update manifest schema {schemaVersion}.");
        }

        var version = RequiredString(root, "version", 1, 64);
        var channel = ParseChannelStrict(RequiredString(root, "channel", 1, 16));
        if (channel != Policy.Channel)
        {
            throw new InvalidDataException(
                $"Update channel '{ChannelName(channel)}' does not match selected channel '{ChannelName(Policy.Channel)}'.");
        }

        var artifact = RequiredString(root, "artifact", 1, 96);
        if (!string.Equals(artifact, "MegasMoves.exe", StringComparison.Ordinal))
        {
            throw new InvalidDataException("Windows update artifact must be exactly MegasMoves.exe.");
        }

        var sizeBytes = RequiredLong(root, "size_bytes");
        if (sizeBytes <= 0 || sizeBytes > MaxUpdateBytes)
        {
            throw new InvalidDataException($"Update artifact must be between 1 byte and {MaxUpdateBytes} bytes.");
        }

        var sha256 = RequiredString(root, "sha256", 64, 64).ToLowerInvariant();
        if (!sha256.All(Uri.IsHexDigit))
        {
            throw new InvalidDataException("Update SHA-256 must contain exactly 64 hexadecimal characters.");
        }

        var urlText = RequiredString(root, "url", 1, 2048);
        if (!Uri.TryCreate(urlText, UriKind.Absolute, out var downloadUri) ||
            !string.Equals(downloadUri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("Update artifact URL must be absolute HTTPS.");
        }

        var signatureAlgorithm = RequiredString(root, "signature_algorithm", 1, 64);
        if (!string.Equals(signatureAlgorithm, SignatureAlgorithm, StringComparison.Ordinal))
        {
            throw new InvalidDataException($"Unsupported update signature algorithm '{signatureAlgorithm}'.");
        }

        byte[] signature;
        try
        {
            signature = Convert.FromBase64String(RequiredString(root, "signature", 1, 2048));
        }
        catch (FormatException ex)
        {
            throw new InvalidDataException("Update signature is not valid base64.", ex);
        }
        if (signature.Length is < 8 or > 512)
        {
            throw new InvalidDataException("Update signature has an invalid length.");
        }

        var signedPayload = Encoding.UTF8.GetBytes(string.Join(
            "\n",
            new[]
            {
                schemaVersion.ToString(System.Globalization.CultureInfo.InvariantCulture),
                version,
                ChannelName(channel),
                artifact,
                sizeBytes.ToString(System.Globalization.CultureInfo.InvariantCulture),
                sha256,
                downloadUri.AbsoluteUri,
                signatureAlgorithm,
                string.Empty,
            }));

        VerifySignature(signedPayload, signature);
        return new VerifiedUpdateManifest(
            schemaVersion,
            version,
            channel,
            artifact,
            sizeBytes,
            sha256,
            downloadUri,
            signatureAlgorithm,
            signature,
            signedPayload);
    }

    public async Task<StagedUpdate> StageAsync(
        string signedManifestJson,
        CancellationToken cancellationToken = default)
    {
        var preflight = await PreflightAsync(cancellationToken);
        if (!preflight.Safe)
        {
            throw new InvalidOperationException($"Update is blocked: {preflight.Summary}");
        }

        var manifest = VerifyManifest(signedManifestJson);
        var stagingDirectory = SafeVersionDirectory("staging", manifest.Version);
        Directory.CreateDirectory(stagingDirectory);
        var partial = Path.Combine(stagingDirectory, "MegasMoves.exe.part");
        var staged = Path.Combine(stagingDirectory, "MegasMoves.exe");

        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, manifest.DownloadUri);
            request.Headers.AcceptEncoding.Clear();
            using var response = await Http.SendAsync(
                request,
                HttpCompletionOption.ResponseHeadersRead,
                cancellationToken);
            response.EnsureSuccessStatusCode();
            if (response.Content.Headers.ContentLength is long announced && announced != manifest.SizeBytes)
            {
                throw new InvalidDataException("Update Content-Length does not match signed metadata.");
            }

            await using var source = await response.Content.ReadAsStreamAsync(cancellationToken);
            await using var target = new FileStream(
                partial,
                FileMode.Create,
                FileAccess.Write,
                FileShare.None,
                bufferSize: 128 * 1024,
                options: FileOptions.Asynchronous | FileOptions.SequentialScan);
            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            var buffer = new byte[128 * 1024];
            long total = 0;
            while (true)
            {
                var read = await source.ReadAsync(buffer.AsMemory(), cancellationToken);
                if (read == 0)
                {
                    break;
                }
                total += read;
                if (total > manifest.SizeBytes || total > MaxUpdateBytes)
                {
                    throw new InvalidDataException("Update download exceeded the signed size bound.");
                }
                hash.AppendData(buffer, 0, read);
                await target.WriteAsync(buffer.AsMemory(0, read), cancellationToken);
            }
            await target.FlushAsync(cancellationToken);

            if (total != manifest.SizeBytes)
            {
                throw new InvalidDataException("Downloaded update size does not match signed metadata.");
            }
            var actualSha = Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
            if (!CryptographicOperations.FixedTimeEquals(
                    Encoding.ASCII.GetBytes(actualSha),
                    Encoding.ASCII.GetBytes(manifest.Sha256)))
            {
                throw new InvalidDataException("Downloaded update SHA-256 does not match signed metadata.");
            }

            File.Move(partial, staged, overwrite: true);
            WindowsPeVerifier.ValidateMegasMovesAmd64Gui(staged);

            var rollback = await SnapshotCurrentBinaryAsync(manifest.Version, cancellationToken);
            return new StagedUpdate(manifest, staged, rollback.Artifact, rollback.Metadata);
        }
        catch
        {
            TryDelete(partial);
            TryDelete(staged);
            throw;
        }
    }

    private async Task<(string Artifact, string Metadata)> SnapshotCurrentBinaryAsync(
        string targetVersion,
        CancellationToken cancellationToken)
    {
        var processPath = Environment.ProcessPath;
        if (string.IsNullOrWhiteSpace(processPath) || !File.Exists(processPath))
        {
            throw new InvalidOperationException("Current MegasMoves executable path is unavailable for rollback snapshotting.");
        }

        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(processPath);
        var rollbackDirectory = SafeVersionDirectory(
            "rollback",
            DateTimeOffset.UtcNow.ToString("yyyyMMdd-HHmmss-fffffff", System.Globalization.CultureInfo.InvariantCulture));
        Directory.CreateDirectory(rollbackDirectory);
        var rollbackArtifact = Path.Combine(rollbackDirectory, "MegasMoves.exe");
        File.Copy(processPath, rollbackArtifact, overwrite: false);
        WindowsPeVerifier.ValidateMegasMovesAmd64Gui(rollbackArtifact);
        var sha = await UpdateApplyWorker.Sha256FileAsync(rollbackArtifact, cancellationToken);
        var metadata = Path.Combine(rollbackDirectory, "rollback.json");
        var payload = JsonSerializer.Serialize(new
        {
            schema_version = 1,
            created_at = DateTimeOffset.UtcNow.ToString("O"),
            source_path = Path.GetFileName(processPath),
            target_version = targetVersion,
            artifact = "MegasMoves.exe",
            size_bytes = new FileInfo(rollbackArtifact).Length,
            sha256 = sha,
        }, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(metadata, payload + Environment.NewLine, new UTF8Encoding(false));
        return (rollbackArtifact, metadata);
    }

    private void VerifySignature(byte[] payload, byte[] signature)
    {
        var trustAnchor = RuntimePayloadResolver.ResolveRuntimeFile("update-public-key.pem");
        if (trustAnchor is null || !File.Exists(trustAnchor))
        {
            trustAnchor = Path.Combine(AppContext.BaseDirectory, "Runtime", "update-public-key.pem");
        }
        if (!File.Exists(trustAnchor))
        {
            trustAnchor = Path.Combine(AppContext.BaseDirectory, "update-public-key.pem");
        }
        if (!File.Exists(trustAnchor))
        {
            throw new InvalidOperationException(
                "Update trust anchor is not installed. Signed update staging is disabled rather than accepting untrusted metadata.");
        }

        var pem = File.ReadAllText(trustAnchor, Encoding.ASCII);
        using var ecdsa = ECDsa.Create();
        ecdsa.ImportFromPem(pem);
        if (ecdsa.KeySize != 256 ||
            !ecdsa.VerifyData(
                payload,
                signature,
                HashAlgorithmName.SHA256,
                DSASignatureFormat.Rfc3279DerSequence))
        {
            throw new CryptographicException("Update metadata signature verification failed.");
        }
    }

    private string SafeVersionDirectory(string category, string version)
    {
        var safe = new string(version.Select(ch => char.IsAsciiLetterOrDigit(ch) || ch is '.' or '-' or '_' ? ch : '_').ToArray());
        if (string.IsNullOrWhiteSpace(safe) || safe.Length > 96)
        {
            throw new InvalidDataException("Update version cannot be mapped to a safe staging directory.");
        }
        var categoryRoot = Path.GetFullPath(Path.Combine(_updatesRoot, category));
        var result = Path.GetFullPath(Path.Combine(categoryRoot, safe));
        if (!result.StartsWith(categoryRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("Update staging path escaped the managed update root.");
        }
        return result;
    }

    private static UpdateChannel ParseChannel(string? value) => value?.Trim().ToLowerInvariant() switch
    {
        "internal" => UpdateChannel.Internal,
        "dev" => UpdateChannel.Dev,
        _ => UpdateChannel.Stable,
    };

    private static UpdateChannel ParseChannelStrict(string value) => value.Trim().ToLowerInvariant() switch
    {
        "stable" => UpdateChannel.Stable,
        "internal" => UpdateChannel.Internal,
        "dev" => UpdateChannel.Dev,
        _ => throw new InvalidDataException($"Unknown update channel '{value}'."),
    };

    public static string ChannelName(UpdateChannel channel) => channel switch
    {
        UpdateChannel.Stable => "stable",
        UpdateChannel.Internal => "internal",
        UpdateChannel.Dev => "dev",
        _ => throw new ArgumentOutOfRangeException(nameof(channel)),
    };

    private static string RequiredString(JsonElement root, string name, int minLength, int maxLength)
    {
        if (!root.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String)
        {
            throw new InvalidDataException($"Update manifest requires string property '{name}'.");
        }
        var text = value.GetString() ?? string.Empty;
        if (text.Length < minLength || text.Length > maxLength)
        {
            throw new InvalidDataException($"Update manifest property '{name}' has an invalid length.");
        }
        return text;
    }

    private static int RequiredInt(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var value) || !value.TryGetInt32(out var result))
        {
            throw new InvalidDataException($"Update manifest requires integer property '{name}'.");
        }
        return result;
    }

    private static long RequiredLong(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var value) || !value.TryGetInt64(out var result))
        {
            throw new InvalidDataException($"Update manifest requires integer property '{name}'.");
        }
        return result;
    }

    private static string Text(JsonElement value, string property) =>
        value.TryGetProperty(property, out var item) && item.ValueKind != JsonValueKind.Null
            ? item.ToString()
            : string.Empty;

    private static int Int(JsonElement value, string property) =>
        value.TryGetProperty(property, out var item) && item.TryGetInt32(out var result)
            ? result
            : 0;

    private static void TryDelete(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch
        {
            // Cleanup is best effort; the next staging attempt overwrites .part.
        }
    }
}
