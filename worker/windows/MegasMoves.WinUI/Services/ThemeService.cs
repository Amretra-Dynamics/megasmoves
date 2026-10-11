using System;
using Microsoft.UI.Xaml;

namespace MegasMoves.Services;

public enum AppThemeMode
{
    System,
    Light,
    Dark,
}

/// <summary>
/// Manages application theme selection (System, Light, Dark) and persists preference
/// to local UI state storage.
/// </summary>
public sealed class ThemeService
{
    public const string ThemePreferenceKey = "theme.mode";
    private readonly UiStateStore _uiState;

    public event Action<AppThemeMode>? ThemeChanged;

    public ThemeService(UiStateStore uiState)
    {
        _uiState = uiState;
    }

    public AppThemeMode CurrentMode
    {
        get
        {
            var raw = _uiState.GetString(ThemePreferenceKey);
            return ParseMode(raw);
        }
    }

    public void SetTheme(AppThemeMode mode)
    {
        var raw = mode switch
        {
            AppThemeMode.Light => "light",
            AppThemeMode.Dark => "dark",
            _ => "system",
        };
        _uiState.SetString(ThemePreferenceKey, raw);
        ThemeChanged?.Invoke(mode);
    }

    public void ToggleTheme()
    {
        var isCurrentlyDark = CurrentMode switch
        {
            AppThemeMode.Dark => true,
            AppThemeMode.Light => false,
            _ => MainWindow.IsSystemDarkTheme(),
        };
        SetTheme(isCurrentlyDark ? AppThemeMode.Light : AppThemeMode.Dark);
    }

    public static AppThemeMode ParseMode(string? value) => value switch
    {
        "light" => AppThemeMode.Light,
        "dark" => AppThemeMode.Dark,
        "system" => AppThemeMode.Dark,
        _ => AppThemeMode.Dark,
    };
}
