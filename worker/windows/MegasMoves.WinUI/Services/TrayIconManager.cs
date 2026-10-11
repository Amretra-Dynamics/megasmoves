using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;

namespace MegasMoves.Services;

public sealed class TrayIconManager : IDisposable
{
    private const int WM_USER = 0x0400;
    public const int WM_TRAYICON = WM_USER + 101;
    private const uint WM_NULL = 0x0000;
    private const uint WM_CLOSE = 0x0010;
    private const uint WM_DESTROY = 0x0002;

    private const int NIM_ADD = 0x00000000;
    private const int NIM_MODIFY = 0x00000001;
    private const int NIM_DELETE = 0x00000002;

    private const int NIF_MESSAGE = 0x00000001;
    private const int NIF_ICON = 0x00000002;
    private const int NIF_TIP = 0x00000004;
    private const int NIF_INFO = 0x00000010;

    private const int NIIF_INFO = 0x00000001;

    private const int WM_LBUTTONUP = 0x0202;
    private const int WM_LBUTTONDBLCLK = 0x0203;
    private const int WM_RBUTTONUP = 0x0205;
    private const int WM_CONTEXTMENU = 0x007B;

    private const uint TPM_RETURNCMD = 0x0100;
    private const uint TPM_NONOTIFY = 0x0080;
    private const uint TPM_RIGHTBUTTON = 0x0002;
    private const uint MF_STRING = 0x00000000;
    private const uint MF_SEPARATOR = 0x00000800;

    private const int ID_MENU_OPEN = 1001;
    private const int ID_MENU_EXIT = 1002;

    private const uint IMAGE_ICON = 1;
    private const uint LR_LOADFROMFILE = 0x0010;
    private const int WS_POPUP = unchecked((int)0x80000000);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NOTIFYICONDATA
    {
        public int cbSize;
        public IntPtr hWnd;
        public int uID;
        public int uFlags;
        public int uCallbackMessage;
        public IntPtr hIcon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string szTip;
        public int dwState;
        public int dwStateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string szInfo;
        public int uTimeoutOrVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
        public string szInfoTitle;
        public int dwInfoFlags;
        public Guid guidItem;
        public IntPtr hBalloonIcon;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT
    {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MSG
    {
        public IntPtr hwnd;
        public uint message;
        public IntPtr wParam;
        public IntPtr lParam;
        public uint time;
        public POINT pt;
        public uint lPrivate;
    }

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate IntPtr WndProc(IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate IntPtr SubclassProc(IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam, UIntPtr uIdSubclass, UIntPtr dwRefData);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WNDCLASSEX
    {
        public int cbSize;
        public uint style;
        public WndProc lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public IntPtr hInstance;
        public IntPtr hIcon;
        public IntPtr hCursor;
        public IntPtr hbrBackground;
        public string? lpszMenuName;
        public string lpszClassName;
        public IntPtr hIconSm;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr OpenInputDesktop(uint dwFlags, bool fInherit, uint dwDesiredAccess);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetThreadDesktop(IntPtr hDesktop);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern ushort RegisterClassEx(ref WNDCLASSEX lpwcx);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool UnregisterClassW(string lpClassName, IntPtr hInstance);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateWindowEx(
        int dwExStyle,
        string lpClassName,
        string lpWindowName,
        int dwStyle,
        int x, int y, int nWidth, int nHeight,
        IntPtr hWndParent,
        IntPtr hMenu,
        IntPtr hInstance,
        IntPtr lpParam);

    [DllImport("user32.dll")]
    private static extern IntPtr DefWindowProcW(IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool DestroyWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool GetMessageW(out MSG lpMsg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax);

    [DllImport("user32.dll")]
    private static extern bool TranslateMessage(ref MSG lpMsg);

    [DllImport("user32.dll")]
    private static extern IntPtr DispatchMessageW(ref MSG lpMsg);

    [DllImport("user32.dll")]
    private static extern void PostQuitMessage(int nExitCode);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool Shell_NotifyIconW(int dwMessage, ref NOTIFYICONDATA lpData);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern uint ExtractIconExW(string szFileName, int nIconIndex, out IntPtr phiconLarge, out IntPtr phiconSmall, uint nIcons);

    [DllImport("comctl32.dll", SetLastError = true)]
    private static extern bool SetWindowSubclass(IntPtr hWnd, SubclassProc pfnSubclass, UIntPtr uIdSubclass, UIntPtr dwRefData);

    [DllImport("comctl32.dll", SetLastError = true)]
    private static extern bool RemoveWindowSubclass(IntPtr hWnd, SubclassProc pfnSubclass, UIntPtr uIdSubclass);

    [DllImport("comctl32.dll", SetLastError = true)]
    private static extern IntPtr DefSubclassProc(IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern IntPtr CreatePopupMenu();

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool AppendMenuW(IntPtr hMenu, uint uFlags, UIntPtr uIDNewItem, string lpNewItem);

    [DllImport("user32.dll")]
    private static extern bool DestroyMenu(IntPtr hMenu);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT lpPoint);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern int TrackPopupMenu(IntPtr hMenu, uint uFlags, int x, int y, int nReserved, IntPtr hWnd, IntPtr prcRect);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr LoadImageW(IntPtr hInst, string name, uint type, int cx, int cy, uint fuLoad);

    [DllImport("user32.dll")]
    private static extern IntPtr LoadIconW(IntPtr hInstance, IntPtr lpIconName);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr hIcon);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern uint RegisterWindowMessageW(string lpString);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandleW(string? lpModuleName);

    private readonly IntPtr _mainHwnd;
    private readonly Action _onOpen;
    private readonly Action _onExit;
    private readonly Func<bool>? _onCloseRequested;
    private readonly SubclassProc _mainSubclassProc;
    private readonly uint _wmTaskbarCreated;

    private readonly Thread _trayThread;
    private readonly ManualResetEventSlim _trayReadyEvent = new(false);
    private readonly WndProc _trayWndProc;

    private IntPtr _trayHWnd = IntPtr.Zero;
    private IntPtr _hIcon = IntPtr.Zero;
    private bool _iconAdded;
    private bool _disposed;
    private string _className = string.Empty;

    private const int TrayId = 4201;

    public TrayIconManager(IntPtr mainWindowHandle, Action onOpen, Action onExit, Func<bool>? onCloseRequested = null)
    {
        _mainHwnd = mainWindowHandle;
        _onOpen = onOpen;
        _onExit = onExit;
        _onCloseRequested = onCloseRequested;
        _wmTaskbarCreated = RegisterWindowMessageW("TaskbarCreated");

        _mainSubclassProc = ProcessMainWindowMessage;
        _trayWndProc = ProcessTrayWindowMessage;

        LogTray($"Initializing TrayIconManager for main HWND={_mainHwnd}");

        if (_mainHwnd != IntPtr.Zero)
        {
            var subclassed = SetWindowSubclass(_mainHwnd, _mainSubclassProc, new UIntPtr(TrayId), UIntPtr.Zero);
            LogTray($"SetWindowSubclass for main HWND result={subclassed}");
        }

        // Start dedicated STA message thread for the system tray icon
        _trayThread = new Thread(TrayThreadEntry)
        {
            IsBackground = true,
            Name = "MegasMoves_TrayThread"
        };
        _trayThread.SetApartmentState(ApartmentState.STA);
        _trayThread.Start();

        // Wait up to 3 seconds for tray window initialization
        _trayReadyEvent.Wait(TimeSpan.FromSeconds(3));
    }

    public static void LogTray(string message)
    {
        try
        {
            var dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Amretra Dynamics",
                "MegasMoves");
            Directory.CreateDirectory(dir);
            File.AppendAllText(Path.Combine(dir, "tray.log"), $"[{DateTime.UtcNow:O}] {message}\n");
        }
        catch { }
    }

    private void TrayThreadEntry()
    {
        try
        {
            // Attach thread to the user's interactive desktop if not already attached
            try
            {
                var hDesk = OpenInputDesktop(0, false, 0x01FF);
                if (hDesk != IntPtr.Zero)
                {
                    var setDesk = SetThreadDesktop(hDesk);
                    LogTray($"SetThreadDesktop result={setDesk}");
                }
            }
            catch (Exception ex)
            {
                LogTray($"OpenInputDesktop/SetThreadDesktop note: {ex.Message}");
            }

            LoadAppIcon();

            var hInstance = GetModuleHandleW(null);
            _className = "MegasMoves_TrayWindow_" + Guid.NewGuid().ToString("N");

            var wcx = new WNDCLASSEX
            {
                cbSize = Marshal.SizeOf<WNDCLASSEX>(),
                lpfnWndProc = _trayWndProc,
                hInstance = hInstance,
                lpszClassName = _className
            };

            var atom = RegisterClassEx(ref wcx);
            LogTray($"RegisterClassEx atom={atom}");

            _trayHWnd = CreateWindowEx(
                0,
                _className,
                "MegasMovesTraySink",
                WS_POPUP,
                0, 0, 0, 0,
                IntPtr.Zero,
                IntPtr.Zero,
                hInstance,
                IntPtr.Zero);

            LogTray($"Created dedicated tray HWND={_trayHWnd}");

            _trayReadyEvent.Set();

            TryAddTrayIcon();

            // Run Win32 message loop on the dedicated tray thread
            while (!_disposed && GetMessageW(out var msg, IntPtr.Zero, 0, 0))
            {
                TranslateMessage(ref msg);
                DispatchMessageW(ref msg);
            }
        }
        catch (Exception ex)
        {
            LogTray($"TrayThreadEntry error: {ex.Message}");
            _trayReadyEvent.Set();
        }
        finally
        {
            RemoveTrayIcon();
            if (_trayHWnd != IntPtr.Zero)
            {
                DestroyWindow(_trayHWnd);
                _trayHWnd = IntPtr.Zero;
            }
            if (!string.IsNullOrEmpty(_className))
            {
                UnregisterClassW(_className, GetModuleHandleW(null));
            }
            LogTray("TrayThreadEntry exited cleanly");
        }
    }

    private void LoadAppIcon()
    {
        try
        {
            var processPath = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(processPath) && File.Exists(processPath))
            {
                var count = ExtractIconExW(processPath, 0, out var hLarge, out var hSmall, 1);
                if (hLarge != IntPtr.Zero)
                {
                    DestroyIcon(hLarge);
                }
                if (hSmall != IntPtr.Zero)
                {
                    _hIcon = hSmall;
                    LogTray($"Loaded icon from ProcessPath via ExtractIconExW: {_hIcon}");
                    return;
                }
            }
        }
        catch (Exception ex)
        {
            LogTray($"ExtractIconExW failed: {ex.Message}");
        }

        try
        {
            var iconCandidate = RuntimePayloadResolver.ResolveAsset("app.ico");
            if (!string.IsNullOrEmpty(iconCandidate) && File.Exists(iconCandidate))
            {
                _hIcon = LoadImageW(IntPtr.Zero, iconCandidate, IMAGE_ICON, 16, 16, LR_LOADFROMFILE);
                if (_hIcon != IntPtr.Zero)
                {
                    LogTray($"Loaded icon from file {iconCandidate}: {_hIcon}");
                    return;
                }
            }

            var baseDir = AppContext.BaseDirectory;
            var candidates = new[]
            {
                Path.Combine(baseDir, "Assets", "app.ico"),
                Path.Combine(baseDir, "app.ico"),
            };

            foreach (var candidate in candidates)
            {
                if (File.Exists(candidate))
                {
                    _hIcon = LoadImageW(IntPtr.Zero, candidate, IMAGE_ICON, 16, 16, LR_LOADFROMFILE);
                    if (_hIcon != IntPtr.Zero)
                    {
                        LogTray($"Loaded icon from file {candidate}: {_hIcon}");
                        return;
                    }
                }
            }
        }
        catch (Exception ex)
        {
            LogTray($"LoadImageW failed: {ex.Message}");
        }

        if (_hIcon == IntPtr.Zero)
        {
            _hIcon = LoadIconW(IntPtr.Zero, (IntPtr)32512); // IDI_APPLICATION
            LogTray($"Fallback to system IDI_APPLICATION icon: {_hIcon}");
        }
    }

    public void EnsureTrayIcon()
    {
        if (_iconAdded || _disposed || _trayHWnd == IntPtr.Zero)
        {
            return;
        }

        if (TryAddTrayIcon())
        {
            return;
        }

        // Retry asynchronously
        Task.Run(async () =>
        {
            for (int i = 0; i < 6 && !_iconAdded && !_disposed; i++)
            {
                await Task.Delay(500);
                if (TryAddTrayIcon())
                {
                    LogTray($"Tray icon added on retry attempt {i + 1}");
                    break;
                }
            }
        });
    }

    public bool TryAddTrayIcon()
    {
        if (_disposed || _trayHWnd == IntPtr.Zero)
        {
            return false;
        }

        if (_hIcon == IntPtr.Zero)
        {
            LoadAppIcon();
        }

        var data = new NOTIFYICONDATA
        {
            cbSize = Marshal.SizeOf<NOTIFYICONDATA>(),
            hWnd = _trayHWnd,
            uID = TrayId,
            uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP,
            uCallbackMessage = WM_TRAYICON,
            hIcon = _hIcon,
            szTip = "MegasMoves"
        };

        LogTray($"Calling Shell_NotifyIconW NIM_ADD: cbSize={data.cbSize}, hWnd={data.hWnd}, hIcon={data.hIcon}");
        _iconAdded = Shell_NotifyIconW(NIM_ADD, ref data);
        var err = Marshal.GetLastWin32Error();
        LogTray($"NIM_ADD result={_iconAdded}, err={err}");

        if (!_iconAdded)
        {
            _iconAdded = Shell_NotifyIconW(NIM_MODIFY, ref data);
            var errModify = Marshal.GetLastWin32Error();
            LogTray($"NIM_MODIFY result={_iconAdded}, err={errModify}");
        }

        return _iconAdded;
    }

    public void ShowBalloonNotification(string title, string message)
    {
        if (_disposed || _trayHWnd == IntPtr.Zero)
        {
            return;
        }

        try
        {
            var data = new NOTIFYICONDATA
            {
                cbSize = Marshal.SizeOf<NOTIFYICONDATA>(),
                hWnd = _trayHWnd,
                uID = TrayId,
                uFlags = NIF_INFO,
                szInfoTitle = title,
                szInfo = message,
                dwInfoFlags = NIIF_INFO,
                uTimeoutOrVersion = 3000
            };

            Shell_NotifyIconW(NIM_MODIFY, ref data);
            LogTray($"ShowBalloonNotification sent: {title}");
        }
        catch (Exception ex)
        {
            LogTray($"ShowBalloonNotification error: {ex.Message}");
        }
    }

    public void RemoveTrayIcon()
    {
        if (!_iconAdded || _trayHWnd == IntPtr.Zero)
        {
            return;
        }

        var data = new NOTIFYICONDATA
        {
            cbSize = Marshal.SizeOf<NOTIFYICONDATA>(),
            hWnd = _trayHWnd,
            uID = TrayId
        };

        Shell_NotifyIconW(NIM_DELETE, ref data);
        _iconAdded = false;
        LogTray("Tray icon removed");
    }

    private IntPtr ProcessTrayWindowMessage(IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam)
    {
        try
        {
            if (_wmTaskbarCreated != 0 && uMsg == _wmTaskbarCreated)
            {
                LogTray("TaskbarCreated received on tray sink, re-ensuring tray icon");
                _iconAdded = false;
                EnsureTrayIcon();
                return IntPtr.Zero;
            }

            if (uMsg == WM_TRAYICON)
            {
                var msg = lParam.ToInt32();
                if (msg == WM_LBUTTONUP || msg == WM_LBUTTONDBLCLK)
                {
                    LogTray($"WM_TRAYICON click (msg={msg:X}), restoring window");
                    _onOpen();
                    return IntPtr.Zero;
                }

                if (msg == WM_RBUTTONUP || msg == WM_CONTEXTMENU)
                {
                    LogTray($"WM_TRAYICON right-click (msg={msg:X}), showing context menu");
                    ShowContextMenu(hWnd);
                    return IntPtr.Zero;
                }
            }
        }
        catch (Exception ex)
        {
            LogTray($"ProcessTrayWindowMessage exception: {ex.Message}");
        }

        return DefWindowProcW(hWnd, uMsg, wParam, lParam);
    }

    private IntPtr ProcessMainWindowMessage(IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam, UIntPtr uIdSubclass, UIntPtr dwRefData)
    {
        try
        {
            if (uMsg == 0x0002 /* WM_DESTROY */ || uMsg == 0x0082 /* WM_NCDESTROY */ || uMsg == 0x0018 /* WM_SHOWWINDOW */)
            {
                LogTray($"ProcessMainWindowMessage: msg=0x{uMsg:X4}, wParam=0x{wParam.ToInt64():X}, lParam=0x{lParam.ToInt64():X}");
            }

            if (uMsg == WM_CLOSE)
            {
                LogTray("WM_CLOSE intercepted in MainWindow SubclassProc");
                if (_onCloseRequested != null && _onCloseRequested())
                {
                    LogTray("WM_CLOSE handled by close-to-tray, preventing window destruction");
                    return IntPtr.Zero;
                }
            }
        }
        catch (Exception ex)
        {
            LogTray($"ProcessMainWindowMessage exception: {ex.Message}");
        }

        return DefSubclassProc(hWnd, uMsg, wParam, lParam);
    }

    private void ShowContextMenu(IntPtr hWnd)
    {
        try
        {
            var hMenu = CreatePopupMenu();
            if (hMenu == IntPtr.Zero)
            {
                return;
            }

            try
            {
                AppendMenuW(hMenu, MF_STRING, new UIntPtr(ID_MENU_OPEN), "Open MegasMoves");
                AppendMenuW(hMenu, MF_SEPARATOR, UIntPtr.Zero, string.Empty);
                AppendMenuW(hMenu, MF_STRING, new UIntPtr(ID_MENU_EXIT), "Exit MegasMoves");

                GetCursorPos(out var pt);
                SetForegroundWindow(hWnd);
                var cmd = TrackPopupMenu(hMenu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON, pt.X, pt.Y, 0, hWnd, IntPtr.Zero);
                PostMessage(hWnd, WM_NULL, IntPtr.Zero, IntPtr.Zero);

                if (cmd == ID_MENU_OPEN)
                {
                    LogTray("Context menu: Open selected");
                    _onOpen();
                }
                else if (cmd == ID_MENU_EXIT)
                {
                    LogTray("Context menu: Exit selected");
                    _onExit();
                }
            }
            finally
            {
                DestroyMenu(hMenu);
            }
        }
        catch (Exception ex)
        {
            LogTray($"ShowContextMenu error: {ex.Message}");
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        LogTray("Disposing TrayIconManager");

        RemoveTrayIcon();

        if (_mainHwnd != IntPtr.Zero)
        {
            RemoveWindowSubclass(_mainHwnd, _mainSubclassProc, new UIntPtr(TrayId));
        }

        if (_trayHWnd != IntPtr.Zero)
        {
            PostMessage(_trayHWnd, WM_CLOSE, IntPtr.Zero, IntPtr.Zero);
        }

        if (_hIcon != IntPtr.Zero)
        {
            DestroyIcon(_hIcon);
            _hIcon = IntPtr.Zero;
        }

        _trayReadyEvent.Dispose();
    }
}
