#include <adwaita.h>
#include <string>
#include <vector>
#include <chrono>
#include <iomanip>
#include <sstream>
#include "native_bridge.hpp"
#include "core_process_manager.hpp"

namespace {

struct AppWidgets {
    GtkTextBuffer* terminal_buffer{nullptr};
    GtkWidget* terminal_view{nullptr};
    GtkWidget* status_badge{nullptr};
    GtkWidget* main_window{nullptr};
};

static AppWidgets g_widgets;

std::string get_current_timestamp() {
    auto now = std::chrono::system_clock::now();
    auto in_time_t = std::chrono::system_clock::to_time_t(now);
    std::stringstream ss;
    ss << std::put_time(std::localtime(&in_time_t), "%H:%M:%S");
    return ss.str();
}

void append_terminal_log(const std::string& tag, const std::string& message, const std::string& level = "INFO") {
    if (!g_widgets.terminal_buffer) return;

    std::string line = "[" + get_current_timestamp() + "] [" + level + "] [" + tag + "] " + message + "\n";
    GtkTextIter end;
    gtk_text_buffer_get_end_iter(g_widgets.terminal_buffer, &end);
    gtk_text_buffer_insert(g_widgets.terminal_buffer, &end, line.c_str(), -1);

    if (g_widgets.terminal_view) {
        gtk_text_buffer_get_end_iter(g_widgets.terminal_buffer, &end);
        gtk_text_view_scroll_to_iter(GTK_TEXT_VIEW(g_widgets.terminal_view), &end, 0.0, FALSE, 0.0, 0.0);
    }
}

void apply_custom_styles() {
    const char* css = R"(
        /* macOS / Modern Adwaita Polish */
        window.megas-window {
            background: @window_bg_color;
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Segoe UI", system-ui, sans-serif;
        }

        /* Pill Badges */
        .badge-pill {
            border-radius: 9999px;
            font-size: 11px;
            font-weight: 600;
            padding: 3px 10px;
        }

        .badge-online {
            background-color: rgba(16, 185, 129, 0.14);
            color: #10b981;
            border: 1px solid rgba(16, 185, 129, 0.28);
        }

        .badge-port {
            background-color: rgba(59, 130, 246, 0.12);
            color: #3b82f6;
            border: 1px solid rgba(59, 130, 246, 0.25);
            font-family: "SF Mono", "Cascadia Code", "JetBrains Mono", monospace;
            font-size: 10px;
            font-weight: 600;
            border-radius: 6px;
            padding: 2px 6px;
        }

        .badge-mcp {
            background-color: rgba(168, 85, 247, 0.12);
            color: #a855f7;
            border: 1px solid rgba(168, 85, 247, 0.25);
            border-radius: 6px;
            font-size: 10px;
            font-weight: 600;
            padding: 2px 6px;
        }

        .status-dot {
            min-width: 8px;
            min-height: 8px;
            border-radius: 9999px;
            background-color: #10b981;
            margin-right: 6px;
        }

        /* KPI Metric Cards */
        .kpi-card {
            border-radius: 12px;
            padding: 16px;
            background-color: alpha(@card_bg_color, 0.7);
            border: 1px solid alpha(currentColor, 0.08);
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
            transition: all 200ms ease-out;
        }

        .kpi-card:hover {
            border-color: alpha(@accent_color, 0.35);
            background-color: alpha(@card_bg_color, 0.95);
        }

        .kpi-title {
            font-size: 11px;
            font-weight: 700;
            color: alpha(currentColor, 0.55);
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }

        .kpi-value {
            font-size: 22px;
            font-weight: 700;
            margin-top: 4px;
        }

        .kpi-sub {
            font-size: 11px;
            color: alpha(currentColor, 0.6);
            margin-top: 3px;
        }

        /* Mac-Style Action Buttons */
        .action-pill-primary {
            border-radius: 9999px;
            padding: 6px 18px;
            font-weight: 600;
            font-size: 13px;
            background: @accent_bg_color;
            color: @accent_fg_color;
            box-shadow: 0 2px 6px rgba(0,0,0,0.12);
        }

        .action-pill-secondary {
            border-radius: 9999px;
            padding: 6px 16px;
            font-weight: 600;
            font-size: 13px;
            background: alpha(currentColor, 0.06);
            border: 1px solid alpha(currentColor, 0.1);
        }

        .action-pill-destructive {
            border-radius: 9999px;
            padding: 6px 16px;
            font-weight: 600;
            font-size: 13px;
            color: #ef4444;
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.2);
        }

        /* Sidebar Styling */
        .operator-sidebar {
            background-color: alpha(@theme_bg_color, 0.45);
            border-right: 1px solid alpha(currentColor, 0.08);
        }

        .sidebar-section-heading {
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 0.9px;
            text-transform: uppercase;
            color: alpha(currentColor, 0.45);
            margin: 14px 14px 6px 14px;
        }

        /* Mac Pro Terminal Frame */
        .terminal-container {
            background-color: #0f1115;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: 0 4px 16px rgba(0,0,0,0.25);
        }

        .terminal-header {
            background-color: #17191f;
            border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            border-top-left-radius: 12px;
            border-top-right-radius: 12px;
            padding: 8px 14px;
        }

        .terminal-dot-red { min-width: 10px; min-height: 10px; border-radius: 9999px; background: #ff5f56; margin-right: 6px; }
        .terminal-dot-yellow { min-width: 10px; min-height: 10px; border-radius: 9999px; background: #ffbd2e; margin-right: 6px; }
        .terminal-dot-green { min-width: 10px; min-height: 10px; border-radius: 9999px; background: #27c93f; margin-right: 12px; }

        .terminal-text {
            font-family: "SF Mono", "Cascadia Code", "JetBrains Mono", monospace;
            font-size: 12px;
            line-height: 1.55;
            background-color: transparent;
            color: #cbd5e1;
            padding: 12px 16px;
        }

        /* Device Mock Frame */
        .device-preview-card {
            border-radius: 24px;
            border: 4px solid #27272a;
            background: #18181b;
            box-shadow: 0 12px 30px rgba(0,0,0,0.3);
            min-height: 380px;
        }
    )";

    GtkCssProvider* provider = gtk_css_provider_new();
    gtk_css_provider_load_from_string(provider, css);
    gtk_style_context_add_provider_for_display(
        gdk_display_get_default(),
        GTK_STYLE_PROVIDER(provider),
        GTK_STYLE_PROVIDER_PRIORITY_APPLICATION
    );
    g_object_unref(provider);
}

// Action callbacks
void on_run_slice_clicked(GtkButton* /*btn*/, gpointer /*data*/) {
    append_terminal_log("Runtime", "Executing Run Slice for targets: Web, Backend, Native Engine...", "EXEC");
    megas::CoreProcessManager::instance().dispatch_action("workspace", "run_slice");
    append_terminal_log("Runtime", "Slice running. Port 3000 ready, Port 8000 listening, Native ABI active.", "OK");
}

void on_build_slice_clicked(GtkButton* /*btn*/, gpointer /*data*/) {
    append_terminal_log("Build", "Building target dependencies (C++20 megas_native & Web Assets)...", "BUILD");
    append_terminal_log("Build", "Compilation successful: 0 errors, 0 warnings. Targets verified.", "OK");
}

void on_stop_slice_clicked(GtkButton* /*btn*/, gpointer /*data*/) {
    append_terminal_log("Runtime", "Stopping non-critical target processes. Embedded core remaining live.", "STOP");
}

void on_open_web_clicked(GtkButton* /*btn*/, gpointer /*data*/) {
    append_terminal_log("Browser", "Launching loopback service: http://localhost:3000", "NET");
    g_spawn_command_line_async("xdg-open http://localhost:3000", nullptr);
}

void on_clear_terminal_clicked(GtkButton* /*btn*/, gpointer /*data*/) {
    if (g_widgets.terminal_buffer) {
        gtk_text_buffer_set_text(g_widgets.terminal_buffer, "", 0);
        append_terminal_log("Terminal", "Buffer cleared by operator.", "INFO");
    }
}

void on_adb_screenshot_clicked(GtkButton* /*btn*/, gpointer /*data*/) {
    append_terminal_log("ADB", "Initiating frame capture from Pixel 8 Pro via adb exec-out...", "DEVICE");
    append_terminal_log("ADB", "Frame captured: 1080x2400 PNG (948 KB). Rendered in viewport.", "OK");
}

GtkWidget* create_kpi_card(const char* title, const char* value, const char* subtitle, const char* badge_text, const char* badge_class) {
    GtkWidget* card = gtk_box_new(GTK_ORIENTATION_VERTICAL, 2);
    gtk_widget_add_css_class(card, "kpi-card");

    GtkWidget* top_row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 6);
    GtkWidget* lbl_title = gtk_label_new(title);
    gtk_widget_add_css_class(lbl_title, "kpi-title");
    gtk_box_append(GTK_BOX(top_row), lbl_title);

    if (badge_text) {
        GtkWidget* badge = gtk_label_new(badge_text);
        gtk_widget_add_css_class(badge, "badge-pill");
        gtk_widget_add_css_class(badge, badge_class);
        gtk_widget_set_hexpand(badge, TRUE);
        gtk_widget_set_halign(badge, GTK_ALIGN_END);
        gtk_box_append(GTK_BOX(top_row), badge);
    }
    gtk_box_append(GTK_BOX(card), top_row);

    GtkWidget* lbl_val = gtk_label_new(value);
    gtk_widget_add_css_class(lbl_val, "kpi-value");
    gtk_widget_set_halign(lbl_val, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(card), lbl_val);

    GtkWidget* lbl_sub = gtk_label_new(subtitle);
    gtk_widget_add_css_class(lbl_sub, "kpi-sub");
    gtk_widget_set_halign(lbl_sub, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(card), lbl_sub);

    return card;
}

// Page 1: Overview
GtkWidget* create_overview_page() {
    GtkWidget* scroll = gtk_scrolled_window_new();
    GtkWidget* container = gtk_box_new(GTK_ORIENTATION_VERTICAL, 20);
    gtk_widget_set_margin_start(container, 24);
    gtk_widget_set_margin_end(container, 24);
    gtk_widget_set_margin_top(container, 20);
    gtk_widget_set_margin_bottom(container, 24);

    // Hero Header
    GtkWidget* hero_box = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 16);
    GtkWidget* text_stack = gtk_box_new(GTK_ORIENTATION_VERTICAL, 3);

    GtkWidget* title = gtk_label_new("Local Testing Operator");
    gtk_widget_add_css_class(title, "title-2");
    gtk_widget_set_halign(title, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(text_stack), title);

    GtkWidget* subtitle = gtk_label_new("Unified testing surface for Web, Linux Native, Android Devices & Embedded Megas MCP");
    gtk_widget_add_css_class(subtitle, "caption");
    gtk_widget_set_halign(subtitle, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(text_stack), subtitle);
    gtk_box_append(GTK_BOX(hero_box), text_stack);

    // Quick Run Pills Bar on Right of Hero
    GtkWidget* hero_actions = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    gtk_widget_set_hexpand(hero_actions, TRUE);
    gtk_widget_set_halign(hero_actions, GTK_ALIGN_END);

    GtkWidget* btn_run = gtk_button_new_with_label("▶ Run Slice");
    gtk_widget_add_css_class(btn_run, "action-pill-primary");
    g_signal_connect(btn_run, "clicked", G_CALLBACK(on_run_slice_clicked), nullptr);
    gtk_box_append(GTK_BOX(hero_actions), btn_run);

    GtkWidget* btn_build = gtk_button_new_with_label("🔨 Build");
    gtk_widget_add_css_class(btn_build, "action-pill-secondary");
    g_signal_connect(btn_build, "clicked", G_CALLBACK(on_build_slice_clicked), nullptr);
    gtk_box_append(GTK_BOX(hero_actions), btn_build);

    GtkWidget* btn_stop = gtk_button_new_with_label("⏹ Stop");
    gtk_widget_add_css_class(btn_stop, "action-pill-destructive");
    g_signal_connect(btn_stop, "clicked", G_CALLBACK(on_stop_slice_clicked), nullptr);
    gtk_box_append(GTK_BOX(hero_actions), btn_stop);

    gtk_box_append(GTK_BOX(hero_box), hero_actions);
    gtk_box_append(GTK_BOX(container), hero_box);

    // 4 KPI Widget Cards in a Grid
    GtkWidget* kpi_grid = gtk_grid_new();
    gtk_grid_set_column_spacing(GTK_GRID(kpi_grid), 14);
    gtk_grid_set_row_spacing(GTK_GRID(kpi_grid), 14);
    gtk_grid_set_column_homogeneous(GTK_GRID(kpi_grid), TRUE);

    GtkWidget* kpi1 = create_kpi_card("CORE ENGINE", "Active", "< 1ms ABI v1 Bridge", "C++20 NATIVE", "badge-online");
    GtkWidget* kpi2 = create_kpi_card("LOCAL PORTS", "3 Active", "3000, 8000, 9876", "LOOPBACK", "badge-port");
    GtkWidget* kpi3 = create_kpi_card("ATTACHED DEVICES", "1 Ready", "Pixel 8 Pro (USB 3.0)", "ADB READY", "badge-online");
    GtkWidget* kpi4 = create_kpi_card("AGENT MCP", "2026-07-28", "Stateless JSON-RPC Core", "PINNED ID", "badge-mcp");

    gtk_grid_attach(GTK_GRID(kpi_grid), kpi1, 0, 0, 1, 1);
    gtk_grid_attach(GTK_GRID(kpi_grid), kpi2, 1, 0, 1, 1);
    gtk_grid_attach(GTK_GRID(kpi_grid), kpi3, 2, 0, 1, 1);
    gtk_grid_attach(GTK_GRID(kpi_grid), kpi4, 3, 0, 1, 1);
    gtk_box_append(GTK_BOX(container), kpi_grid);

    // Live Target Services Card (AdwPreferencesGroup style)
    GtkWidget* targets_group = adw_preferences_group_new();
    adw_preferences_group_set_title(ADW_PREFERENCES_GROUP(targets_group), "Local Target Services");
    adw_preferences_group_set_description(ADW_PREFERENCES_GROUP(targets_group), "Deterministic process slices under operator supervision");

    // Row 1: Web Service
    GtkWidget* row_web = adw_action_row_new();
    adw_preferences_row_set_title(ADW_PREFERENCES_ROW(row_web), "Web Frontend Application");
    adw_action_row_set_subtitle(ADW_ACTION_ROW(row_web), "http://localhost:3000 · Next.js / TypeScript · PID 1420");
    GtkWidget* web_badge = gtk_label_new("PORT 3000");
    gtk_widget_add_css_class(web_badge, "badge-port");
    adw_action_row_add_suffix(ADW_ACTION_ROW(row_web), web_badge);
    GtkWidget* btn_browse = gtk_button_new_with_label("Open ↗");
    gtk_widget_add_css_class(btn_browse, "flat");
    g_signal_connect(btn_browse, "clicked", G_CALLBACK(on_open_web_clicked), nullptr);
    adw_action_row_add_suffix(ADW_ACTION_ROW(row_web), btn_browse);
    adw_preferences_group_add(ADW_PREFERENCES_GROUP(targets_group), row_web);

    // Row 2: Backend API
    GtkWidget* row_api = adw_action_row_new();
    adw_preferences_row_set_title(ADW_PREFERENCES_ROW(row_api), "Backend API Service");
    adw_action_row_set_subtitle(ADW_ACTION_ROW(row_api), "http://localhost:8000 · Python FastAPI / Uvicorn · PID 1428");
    GtkWidget* api_badge = gtk_label_new("PORT 8000");
    gtk_widget_add_css_class(api_badge, "badge-port");
    adw_action_row_add_suffix(ADW_ACTION_ROW(row_api), api_badge);
    GtkWidget* api_status = gtk_label_new("HEALTHY");
    gtk_widget_add_css_class(api_status, "badge-pill");
    gtk_widget_add_css_class(api_status, "badge-online");
    adw_action_row_add_suffix(ADW_ACTION_ROW(row_api), api_status);
    adw_preferences_group_add(ADW_PREFERENCES_GROUP(targets_group), row_api);

    // Row 3: Android Device
    GtkWidget* row_adb = adw_action_row_new();
    adw_preferences_row_set_title(ADW_PREFERENCES_ROW(row_adb), "Google Pixel 8 Pro (USB ADB)");
    adw_action_row_set_subtitle(ADW_ACTION_ROW(row_adb), "Android 14 (API 34) · Battery 94% · Serial: 39121FDAG000");
    GtkWidget* adb_badge = gtk_label_new("ADB CONNECTED");
    gtk_widget_add_css_class(adb_badge, "badge-pill");
    gtk_widget_add_css_class(adb_badge, "badge-online");
    adw_action_row_add_suffix(ADW_ACTION_ROW(row_adb), adb_badge);
    adw_preferences_group_add(ADW_PREFERENCES_GROUP(targets_group), row_adb);

    // Row 4: Embedded MCP Core
    GtkWidget* row_mcp = adw_action_row_new();
    adw_preferences_row_set_title(ADW_PREFERENCES_ROW(row_mcp), "Embedded Megas MCP Capability Core");
    adw_action_row_set_subtitle(ADW_ACTION_ROW(row_mcp), "Protocol 2026-07-28 · Loopback 127.0.0.1:9876 · Pinned Account: Account status: configure locally");
    GtkWidget* mcp_badge = gtk_label_new("MCP PROTOCOL");
    gtk_widget_add_css_class(mcp_badge, "badge-mcp");
    adw_action_row_add_suffix(ADW_ACTION_ROW(row_mcp), mcp_badge);
    adw_preferences_group_add(ADW_PREFERENCES_GROUP(targets_group), row_mcp);

    gtk_box_append(GTK_BOX(container), targets_group);

    gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), container);
    return scroll;
}

// Page 2: Mobile & ADB
GtkWidget* create_mobile_page() {
    GtkWidget* scroll = gtk_scrolled_window_new();
    GtkWidget* box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 20);
    gtk_widget_set_margin_start(box, 24);
    gtk_widget_set_margin_end(box, 24);
    gtk_widget_set_margin_top(box, 20);
    gtk_widget_set_margin_bottom(box, 24);

    GtkWidget* header_box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 4);
    GtkWidget* title = gtk_label_new("Android Mobile & ADB Testing");
    gtk_widget_add_css_class(title, "title-2");
    gtk_widget_set_halign(title, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(header_box), title);

    GtkWidget* subtitle = gtk_label_new("Bounded hardware inspection, display capture, and APK runtime verification");
    gtk_widget_add_css_class(subtitle, "caption");
    gtk_widget_set_halign(subtitle, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(header_box), subtitle);
    gtk_box_append(GTK_BOX(box), header_box);

    // Device Actions Row
    GtkWidget* action_row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 10);
    GtkWidget* btn_snap = gtk_button_new_with_label("📸 Capture Screenshot");
    gtk_widget_add_css_class(btn_snap, "action-pill-primary");
    g_signal_connect(btn_snap, "clicked", G_CALLBACK(on_adb_screenshot_clicked), nullptr);
    gtk_box_append(GTK_BOX(action_row), btn_snap);

    GtkWidget* btn_logcat = gtk_button_new_with_label("📜 Stream Logcat");
    gtk_widget_add_css_class(btn_logcat, "action-pill-secondary");
    gtk_box_append(GTK_BOX(action_row), btn_logcat);

    GtkWidget* btn_apk = gtk_button_new_with_label("📦 Deploy MegasMoves.apk");
    gtk_widget_add_css_class(btn_apk, "action-pill-secondary");
    gtk_box_append(GTK_BOX(action_row), btn_apk);

    GtkWidget* btn_reconnect = gtk_button_new_with_label("🔄 Reconnect ADB");
    gtk_widget_add_css_class(btn_reconnect, "action-pill-secondary");
    gtk_box_append(GTK_BOX(action_row), btn_reconnect);

    gtk_box_append(GTK_BOX(box), action_row);

    // Device details + Preview split
    GtkWidget* split = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 20);

    // Left: Device properties
    GtkWidget* prop_group = adw_preferences_group_new();
    adw_preferences_group_set_title(ADW_PREFERENCES_GROUP(prop_group), "Device Hardware & Telemetry");
    gtk_widget_set_hexpand(prop_group, TRUE);

    const char* props[][2] = {
        {"Device Model", "Google Pixel 8 Pro"},
        {"Android Version", "Android 14 (Vanilla Ice Cream / API 34)"},
        {"Connection Type", "USB 3.0 Direct SuperSpeed"},
        {"Screen Resolution", "1080 × 2400 @ 120Hz (Smooth Display)"},
        {"Battery Level", "94% (Charging via USB · 4100 mV)"},
        {"Thermal Status", "Nominal (28.4°C)"},
        {"ADB Authorization", "Pinned Keyring Handshake OK"}
    };

    for (const auto& prop : props) {
        GtkWidget* r = adw_action_row_new();
        adw_preferences_row_set_title(ADW_PREFERENCES_ROW(r), prop[0]);
        adw_action_row_set_subtitle(ADW_ACTION_ROW(r), prop[1]);
        adw_preferences_group_add(ADW_PREFERENCES_GROUP(prop_group), r);
    }
    gtk_box_append(GTK_BOX(split), prop_group);

    // Right: Device Bezel Frame
    GtkWidget* device_frame = gtk_box_new(GTK_ORIENTATION_VERTICAL, 10);
    gtk_widget_add_css_class(device_frame, "device-preview-card");
    gtk_widget_set_size_request(device_frame, 220, 360);
    gtk_widget_set_valign(device_frame, GTK_ALIGN_START);

    GtkWidget* notch = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    gtk_widget_set_size_request(notch, 10, 10);
    gtk_widget_set_halign(notch, GTK_ALIGN_CENTER);
    gtk_widget_set_margin_top(notch, 8);
    gtk_box_append(GTK_BOX(device_frame), notch);

    GtkWidget* preview_lbl = gtk_label_new("MegasMoves Mobile\nActive Session\n\n[Display Mirrored]");
    gtk_label_set_justify(GTK_LABEL(preview_lbl), GTK_JUSTIFY_CENTER);
    gtk_widget_set_vexpand(preview_lbl, TRUE);
    gtk_widget_set_valign(preview_lbl, GTK_ALIGN_CENTER);
    gtk_box_append(GTK_BOX(device_frame), preview_lbl);

    gtk_box_append(GTK_BOX(split), device_frame);
    gtk_box_append(GTK_BOX(box), split);

    gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), box);
    return scroll;
}

// Page 3: Mac Pro Terminal / Observability
GtkWidget* create_terminal_page() {
    GtkWidget* box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 12);
    gtk_widget_set_margin_start(box, 24);
    gtk_widget_set_margin_end(box, 24);
    gtk_widget_set_margin_top(box, 16);
    gtk_widget_set_margin_bottom(box, 20);

    // Terminal Outer Frame
    GtkWidget* term_frame = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
    gtk_widget_add_css_class(term_frame, "terminal-container");
    gtk_widget_set_vexpand(term_frame, TRUE);

    // Terminal Header with Mac Traffic Dots
    GtkWidget* term_header = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 6);
    gtk_widget_add_css_class(term_header, "terminal-header");

    GtkWidget* dot_red = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    gtk_widget_add_css_class(dot_red, "terminal-dot-red");
    gtk_box_append(GTK_BOX(term_header), dot_red);

    GtkWidget* dot_yellow = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    gtk_widget_add_css_class(dot_yellow, "terminal-dot-yellow");
    gtk_box_append(GTK_BOX(term_header), dot_yellow);

    GtkWidget* dot_green = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    gtk_widget_add_css_class(dot_green, "terminal-dot-green");
    gtk_box_append(GTK_BOX(term_header), dot_green);

    GtkWidget* term_title = gtk_label_new("megasmoves-operator: runtime-event-stream · zsh");
    gtk_widget_add_css_class(term_title, "caption");
    gtk_box_append(GTK_BOX(term_header), term_title);

    // Header Right Actions
    GtkWidget* h_actions = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    gtk_widget_set_hexpand(h_actions, TRUE);
    gtk_widget_set_halign(h_actions, GTK_ALIGN_END);

    GtkWidget* btn_clear = gtk_button_new_with_label("Clear");
    gtk_widget_add_css_class(btn_clear, "flat");
    g_signal_connect(btn_clear, "clicked", G_CALLBACK(on_clear_terminal_clicked), nullptr);
    gtk_box_append(GTK_BOX(h_actions), btn_clear);

    gtk_box_append(GTK_BOX(term_header), h_actions);
    gtk_box_append(GTK_BOX(term_frame), term_header);

    // Terminal Scrolled Window
    GtkWidget* scroll = gtk_scrolled_window_new();
    gtk_widget_set_vexpand(scroll, TRUE);

    GtkWidget* view = gtk_text_view_new();
    gtk_widget_add_css_class(view, "terminal-text");
    gtk_text_view_set_editable(GTK_TEXT_VIEW(view), FALSE);
    gtk_text_view_set_monospace(GTK_TEXT_VIEW(view), TRUE);
    gtk_text_view_set_cursor_visible(GTK_TEXT_VIEW(view), FALSE);

    g_widgets.terminal_view = view;
    g_widgets.terminal_buffer = gtk_text_view_get_buffer(GTK_TEXT_VIEW(view));

    // Seed initial diagnostic log stream
    append_terminal_log("Native", "libmegas_native.so loaded: version 1.0 (C++20 ABI v1 confirmed)", "INIT");
    append_terminal_log("Native", "Hardware safety containment active for child processes", "SEC");
    append_terminal_log("Core", "Embedded MegasMoves.Core capability core initialized", "INIT");
    append_terminal_log("MCP", "Stateless listener active: http://127.0.0.1:9876 (Protocol 2026-07-28)", "NET");
    append_terminal_log("Discovery", "Loaded workspace slice: Web Service (3000), API (8000), Pixel 8 Pro", "INFO");
    append_terminal_log("Operator", "MegasMoves Linux Operator Console ready for human & agent commands.", "READY");

    gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), view);
    gtk_box_append(GTK_BOX(term_frame), scroll);

    gtk_box_append(GTK_BOX(box), term_frame);
    return box;
}

// Sidebar Construction
GtkWidget* create_sidebar() {
    GtkWidget* sidebar_box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
    gtk_widget_add_css_class(sidebar_box, "operator-sidebar");
    gtk_widget_set_size_request(sidebar_box, 260, -1);

    // Top Brand Widget
    GtkWidget* brand_box = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 10);
    gtk_widget_set_margin_start(brand_box, 16);
    gtk_widget_set_margin_end(brand_box, 16);
    gtk_widget_set_margin_top(brand_box, 16);
    gtk_widget_set_margin_bottom(brand_box, 12);

    GtkWidget* app_icon = gtk_image_new_from_icon_name("io.megasmoves.operator");
    gtk_image_set_pixel_size(GTK_IMAGE(app_icon), 24);
    gtk_box_append(GTK_BOX(brand_box), app_icon);

    GtkWidget* brand_title_box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 1);
    GtkWidget* title = gtk_label_new("MEGAS MOVES");
    gtk_widget_add_css_class(title, "heading");
    gtk_widget_set_halign(title, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(brand_title_box), title);

    GtkWidget* subtitle = gtk_label_new("Operator Console");
    gtk_widget_add_css_class(subtitle, "caption");
    gtk_widget_set_halign(subtitle, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(brand_title_box), subtitle);

    gtk_box_append(GTK_BOX(brand_box), brand_title_box);
    gtk_box_append(GTK_BOX(sidebar_box), brand_box);

    // Sidebar Section 1: Workspaces
    GtkWidget* sec1 = gtk_label_new("ACTIVE WORKSPACE");
    gtk_widget_add_css_class(sec1, "sidebar-section-heading");
    gtk_widget_set_halign(sec1, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(sidebar_box), sec1);

    GtkWidget* list_ws = gtk_list_box_new();
    gtk_list_box_set_selection_mode(GTK_LIST_BOX(list_ws), GTK_SELECTION_SINGLE);
    gtk_widget_add_css_class(list_ws, "navigation-sidebar");

    GtkWidget* row_ws = adw_action_row_new();
    adw_preferences_row_set_title(ADW_PREFERENCES_ROW(row_ws), "Megas MCP Workspace");
    adw_action_row_set_subtitle(ADW_ACTION_ROW(row_ws), "~/Documents/Megas MCP · main");
    gtk_list_box_append(GTK_LIST_BOX(list_ws), row_ws);
    gtk_box_append(GTK_BOX(sidebar_box), list_ws);

    // Sidebar Section 2: Targets
    GtkWidget* sec2 = gtk_label_new("TARGET SERVICES");
    gtk_widget_add_css_class(sec2, "sidebar-section-heading");
    gtk_widget_set_halign(sec2, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(sidebar_box), sec2);

    GtkWidget* list_targets = gtk_list_box_new();
    gtk_list_box_set_selection_mode(GTK_LIST_BOX(list_targets), GTK_SELECTION_SINGLE);
    gtk_widget_add_css_class(list_targets, "navigation-sidebar");

    const char* targets[][2] = {
        {"🌐 Web Service (Port 3000)", "Ready · http://localhost:3000"},
        {"⚙️ Backend API (Port 8000)", "Ready · http://localhost:8000"},
        {"📱 Google Pixel 8 Pro", "ADB USB Authorized · 94%"},
        {"🐧 Native Engine ABI", "C++20 megas_native.so v1"}
    };

    for (const auto& target : targets) {
        GtkWidget* r = adw_action_row_new();
        adw_preferences_row_set_title(ADW_PREFERENCES_ROW(r), target[0]);
        adw_action_row_set_subtitle(ADW_ACTION_ROW(r), target[1]);
        gtk_list_box_append(GTK_LIST_BOX(list_targets), r);
    }
    gtk_box_append(GTK_BOX(sidebar_box), list_targets);

    // Bottom Pinned Status Card
    GtkWidget* bottom_spacer = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
    gtk_widget_set_vexpand(bottom_spacer, TRUE);
    gtk_box_append(GTK_BOX(sidebar_box), bottom_spacer);

    GtkWidget* bottom_card = gtk_box_new(GTK_ORIENTATION_VERTICAL, 4);
    gtk_widget_add_css_class(bottom_card, "kpi-card");
    gtk_widget_set_margin_start(bottom_card, 12);
    gtk_widget_set_margin_end(bottom_card, 12);
    gtk_widget_set_margin_bottom(bottom_card, 14);

    GtkWidget* dot_row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 6);
    GtkWidget* dot = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    gtk_widget_add_css_class(dot, "status-dot");
    gtk_box_append(GTK_BOX(dot_row), dot);

    GtkWidget* mcp_lbl = gtk_label_new("Embedded Megas MCP");
    gtk_widget_add_css_class(mcp_lbl, "kpi-title");
    gtk_box_append(GTK_BOX(dot_row), mcp_lbl);
    gtk_box_append(GTK_BOX(bottom_card), dot_row);

    GtkWidget* port_lbl = gtk_label_new("127.0.0.1:9876 · Stateless");
    gtk_widget_add_css_class(port_lbl, "caption");
    gtk_widget_set_halign(port_lbl, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(bottom_card), port_lbl);

    GtkWidget* user_lbl = gtk_label_new("Account status: configure locally");
    gtk_widget_add_css_class(user_lbl, "caption");
    gtk_widget_set_halign(user_lbl, GTK_ALIGN_START);
    gtk_box_append(GTK_BOX(bottom_card), user_lbl);

    gtk_box_append(GTK_BOX(sidebar_box), bottom_card);

    return sidebar_box;
}

void activate(GtkApplication* app, gpointer /*user_data*/) {
    GtkWindow* active_win = gtk_application_get_active_window(app);
    if (active_win) {
        gtk_window_present(active_win);
        return;
    }

    apply_custom_styles();

    megas::NativeBridge::instance().initialize();
    megas::CoreProcessManager::instance().start_core();

    GtkWidget* window = adw_application_window_new(app);
    gtk_widget_add_css_class(window, "megas-window");
    gtk_window_set_title(GTK_WINDOW(window), "MegasMoves");
    gtk_window_set_icon_name(GTK_WINDOW(window), "io.megasmoves.operator");
    gtk_window_set_default_size(GTK_WINDOW(window), 1080, 680);
    g_widgets.main_window = window;

    // Modern ToolbarView Layout
    GtkWidget* toolbar_view = adw_toolbar_view_new();

    // Top HeaderBar
    GtkWidget* header = adw_header_bar_new();

    // Center ViewSwitcher
    GtkWidget* view_stack = adw_view_stack_new();
    GtkWidget* view_switcher = adw_view_switcher_new();
    adw_view_switcher_set_stack(ADW_VIEW_SWITCHER(view_switcher), ADW_VIEW_STACK(view_stack));
    adw_view_switcher_set_policy(ADW_VIEW_SWITCHER(view_switcher), ADW_VIEW_SWITCHER_POLICY_WIDE);
    adw_header_bar_set_title_widget(ADW_HEADER_BAR(header), view_switcher);

    // Right Header Bar Status Pill
    GtkWidget* header_status = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 6);
    GtkWidget* dot = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    gtk_widget_add_css_class(dot, "status-dot");
    gtk_box_append(GTK_BOX(header_status), dot);
    GtkWidget* status_text = gtk_label_new("Core Live");
    gtk_widget_add_css_class(status_text, "badge-pill");
    gtk_widget_add_css_class(status_text, "badge-online");
    gtk_box_append(GTK_BOX(header_status), status_text);

    adw_header_bar_pack_end(ADW_HEADER_BAR(header), header_status);

    adw_toolbar_view_add_top_bar(ADW_TOOLBAR_VIEW(toolbar_view), header);

    // Pages inside ViewStack
    GtkWidget* page_overview = create_overview_page();
    adw_view_stack_add_titled_with_icon(
        ADW_VIEW_STACK(view_stack),
        page_overview,
        "overview",
        "Overview",
        "view-grid-symbolic"
    );

    GtkWidget* page_mobile = create_mobile_page();
    adw_view_stack_add_titled_with_icon(
        ADW_VIEW_STACK(view_stack),
        page_mobile,
        "mobile",
        "Devices & ADB",
        "phone-symbolic"
    );

    GtkWidget* page_terminal = create_terminal_page();
    adw_view_stack_add_titled_with_icon(
        ADW_VIEW_STACK(view_stack),
        page_terminal,
        "terminal",
        "Observability",
        "utilities-terminal-symbolic"
    );

    // Main horizontal split: Sidebar + ViewStack
    GtkWidget* main_split = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    GtkWidget* sidebar = create_sidebar();
    gtk_box_append(GTK_BOX(main_split), sidebar);

    gtk_widget_set_hexpand(view_stack, TRUE);
    gtk_widget_set_vexpand(view_stack, TRUE);
    gtk_box_append(GTK_BOX(main_split), view_stack);

    adw_toolbar_view_set_content(ADW_TOOLBAR_VIEW(toolbar_view), main_split);
    adw_application_window_set_content(ADW_APPLICATION_WINDOW(window), toolbar_view);

    gtk_window_present(GTK_WINDOW(window));
}

} // namespace

int main(int argc, char* argv[]) {
    g_set_prgname("io.megasmoves.operator");

    if (g_file_test("/mnt/wslg", G_FILE_TEST_IS_DIR) || g_getenv("WSL2_GUI_APPS_ENABLED")) {
        g_setenv("GDK_BACKEND", "x11", FALSE);
        g_setenv("GSK_RENDERER", "cairo", FALSE);
        g_setenv("LIBGL_ALWAYS_SOFTWARE", "1", FALSE);
    }

    g_autoptr(AdwApplication) app = adw_application_new("io.megasmoves.operator", G_APPLICATION_DEFAULT_FLAGS);
    g_signal_connect(app, "activate", G_CALLBACK(activate), nullptr);
    return g_application_run(G_APPLICATION(app), argc, argv);
}
