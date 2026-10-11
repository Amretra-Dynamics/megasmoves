using Microsoft.Data.Sqlite;

namespace MegasMoves.Services;

/// <summary>
/// Versioned, non-authoritative UI-local state for the WinUI shell.
///
/// This store deliberately owns only presentation/recovery preferences such as
/// window geometry and the last selected surface. Megas MCP remains authoritative
/// for machines, sessions, tasks, operations, permissions and evidence; MegasMoves
/// remains authoritative for its own application/domain state.
/// </summary>
public sealed class UiStateStore
{
    public const int CurrentSchemaVersion = 1;
    private const int MaxKeyLength = 128;
    private const int MaxValueLength = 16 * 1024;

    private readonly string _connectionString;
    private readonly object _gate = new();

    public UiStateStore()
    {
        var root = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Amretra Dynamics",
            "MegasMoves");
        Directory.CreateDirectory(root);
        DatabasePath = Path.Combine(root, "ui-state.db");
        _connectionString = new SqliteConnectionStringBuilder
        {
            DataSource = DatabasePath,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Cache = SqliteCacheMode.Private,
        }.ToString();

        try
        {
            EnsureSchema();
            IsAvailable = true;
        }
        catch (Exception ex) when (ex is SqliteException or IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            // UI-local persistence must never take the operator core down. Surface
            // the failure diagnostically and continue with safe in-memory defaults.
            LastError = ex.Message;
            IsAvailable = false;
        }
    }

    public string DatabasePath { get; }
    public bool IsAvailable { get; private set; }
    public string? LastError { get; private set; }

    public string? GetString(string key)
    {
        ValidateKey(key);
        if (!IsAvailable)
        {
            return null;
        }

        lock (_gate)
        {
            try
            {
                using var connection = Open();
                using var command = connection.CreateCommand();
                command.CommandText = "SELECT value FROM preferences WHERE key = $key LIMIT 1";
                command.Parameters.AddWithValue("$key", key);
                return command.ExecuteScalar() as string;
            }
            catch (SqliteException ex)
            {
                Degrade(ex);
                return null;
            }
        }
    }

    public int? GetInt32(string key)
    {
        var raw = GetString(key);
        return int.TryParse(raw, out var value) ? value : null;
    }

    public bool? GetBool(string key)
    {
        var raw = GetString(key);
        return bool.TryParse(raw, out var value) ? value : null;
    }

    public void SetBool(string key, bool value) => SetString(key, value ? "true" : "false");

    public void SetString(string key, string value)
    {
        ValidateKey(key);
        ArgumentNullException.ThrowIfNull(value);
        if (value.Length > MaxValueLength)
        {
            throw new ArgumentOutOfRangeException(nameof(value), $"UI state values are limited to {MaxValueLength} characters");
        }
        if (!IsAvailable)
        {
            return;
        }

        lock (_gate)
        {
            try
            {
                using var connection = Open();
                using var command = connection.CreateCommand();
                command.CommandText = """
                    INSERT INTO preferences(key, value, updated_at)
                    VALUES($key, $value, $updated_at)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """;
                command.Parameters.AddWithValue("$key", key);
                command.Parameters.AddWithValue("$value", value);
                command.Parameters.AddWithValue("$updated_at", DateTimeOffset.UtcNow.ToString("O"));
                command.ExecuteNonQuery();
            }
            catch (SqliteException ex)
            {
                Degrade(ex);
            }
        }
    }

    public void SetInt32(string key, int value) => SetString(key, value.ToString(System.Globalization.CultureInfo.InvariantCulture));

    private SqliteConnection Open()
    {
        var connection = new SqliteConnection(_connectionString);
        connection.Open();
        using var pragma = connection.CreateCommand();
        pragma.CommandText = "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=3000;";
        pragma.ExecuteNonQuery();
        return connection;
    }

    private void EnsureSchema()
    {
        lock (_gate)
        {
            using var connection = Open();
            using var transaction = connection.BeginTransaction();

            using (var command = connection.CreateCommand())
            {
                command.Transaction = transaction;
                command.CommandText = """
                    CREATE TABLE IF NOT EXISTS schema_info(
                        singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
                        version INTEGER NOT NULL
                    );
                    INSERT INTO schema_info(singleton, version)
                    VALUES(1, 0)
                    ON CONFLICT(singleton) DO NOTHING;
                    """;
                command.ExecuteNonQuery();
            }

            int version;
            using (var command = connection.CreateCommand())
            {
                command.Transaction = transaction;
                command.CommandText = "SELECT version FROM schema_info WHERE singleton = 1";
                version = Convert.ToInt32(command.ExecuteScalar(), System.Globalization.CultureInfo.InvariantCulture);
            }

            if (version > CurrentSchemaVersion)
            {
                throw new InvalidOperationException(
                    $"UI state database schema {version} is newer than supported schema {CurrentSchemaVersion}");
            }

            if (version < 1)
            {
                using var command = connection.CreateCommand();
                command.Transaction = transaction;
                command.CommandText = """
                    CREATE TABLE IF NOT EXISTS preferences(
                        key TEXT NOT NULL PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    UPDATE schema_info SET version = 1 WHERE singleton = 1;
                    """;
                command.ExecuteNonQuery();
                version = 1;
            }

            if (version != CurrentSchemaVersion)
            {
                throw new InvalidOperationException($"UI state migration stopped at unsupported schema {version}");
            }

            transaction.Commit();
        }
    }

    private static void ValidateKey(string key)
    {
        if (string.IsNullOrWhiteSpace(key) || key.Length > MaxKeyLength)
        {
            throw new ArgumentException($"UI state keys must contain 1-{MaxKeyLength} characters", nameof(key));
        }
        foreach (var ch in key)
        {
            if (!(char.IsAsciiLetterOrDigit(ch) || ch is '.' or '_' or '-'))
            {
                throw new ArgumentException("UI state keys may contain only ASCII letters, digits, dot, underscore and hyphen", nameof(key));
            }
        }
    }

    private void Degrade(Exception error)
    {
        LastError = error.Message;
        IsAvailable = false;
    }
}
