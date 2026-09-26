use base64::{engine::general_purpose::STANDARD, Engine as _};
#[cfg(not(target_os = "macos"))]
use enigo::Axis;
use enigo::{Button, Coordinate, Direction, Enigo, Key, Keyboard, Mouse, Settings};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::Cursor;
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
use std::process::Command;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use xcap::image::{imageops::FilterType, DynamicImage, ImageFormat, RgbaImage};
use xcap::{Monitor, Window};

#[cfg(target_os = "windows")]
mod windows_focus;

#[cfg(target_os = "macos")]
use core_foundation::array::{CFArray, CFArrayRef};
#[cfg(target_os = "macos")]
use core_foundation::base::{CFType, CFTypeRef, TCFType};
#[cfg(target_os = "macos")]
use core_foundation::boolean::CFBoolean;
#[cfg(target_os = "macos")]
use core_foundation::dictionary::{CFDictionary, CFDictionaryGetValue, CFDictionaryRef};
#[cfg(target_os = "macos")]
use core_foundation::number::CFNumber;
#[cfg(target_os = "macos")]
use core_foundation::string::{CFString, CFStringRef};
#[cfg(target_os = "macos")]
use core_graphics::color_space::{kCGColorSpaceSRGB, CGColorSpace};
#[cfg(target_os = "macos")]
use core_graphics::context::{CGContext, CGInterpolationQuality};
#[cfg(target_os = "macos")]
use core_graphics::display::CGRectNull;
#[cfg(target_os = "macos")]
use core_graphics::event::{
    CGEvent, CGEventFlags, CGEventTapLocation, CGEventType, CGMouseButton, EventField, KeyCode,
    ScrollEventUnit,
};
#[cfg(target_os = "macos")]
use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};
#[cfg(target_os = "macos")]
use core_graphics::geometry::{CGPoint, CGRect, CGSize};
#[cfg(target_os = "macos")]
use core_graphics::image::{CGImage, CGImageAlphaInfo, CGImageByteOrderInfo};
#[cfg(target_os = "macos")]
use core_graphics::window::{
    copy_window_info, create_image, kCGNullWindowID, kCGWindowImageBoundsIgnoreFraming,
    kCGWindowListExcludeDesktopElements, kCGWindowListOptionAll,
    kCGWindowListOptionIncludingWindow,
};
#[cfg(target_os = "macos")]
use foreign_types::ForeignType;

#[cfg(all(test, target_os = "macos"))]
mod background_experiment;
#[cfg(target_os = "macos")]
mod background_input;
mod diagnostics;
#[cfg(target_os = "macos")]
mod mac_accessibility;
#[cfg(target_os = "macos")]
mod mac_ax_relations;
mod policy;
#[cfg(all(test, target_os = "macos"))]
mod regression_tests;
#[cfg(target_os = "macos")]
mod window_lifecycle;
#[cfg(target_os = "macos")]
mod window_observation;
#[cfg(target_os = "macos")]
mod window_relations;
use policy::{DeliveryPolicy, TargetScope};
#[cfg(target_os = "macos")]
use window_relations::{WindowKind, WindowRelations};

const MAX_SCREENSHOT_BYTES: usize = 20 * 1024 * 1024;
const MAX_SCROLL_DELTA: i32 = 10_000;
#[cfg(target_os = "macos")]
const SCROLL_STEP_PIXELS: i32 = 50;
const SCROLL_SETTLE_MS: u64 = 180;
#[cfg(target_os = "macos")]
const CLICK_SETTLE_MS: u64 = 250;

fn strict_background_input_available() -> bool {
    #[cfg(target_os = "macos")]
    {
        background_input::available()
    }
    #[cfg(not(target_os = "macos"))]
    {
        false
    }
}

fn strict_background_reason() -> &'static str {
    if cfg!(target_os = "macos") {
        "Strict input requires an exact validated OS/app/action profile and live isolation checks; unsupported targets are rejected without foreground fallback"
    } else {
        "Strict background input requires macOS window targeting"
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct DisplayInfo {
    id: String,
    name: String,
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    primary: bool,
}

#[derive(Debug, Serialize)]
pub struct ComputerUseCapabilities {
    gui_available: bool,
    input_available: bool,
    capture_available: bool,
    ax_available: bool,
    ax_protocol_version: Option<u8>,
    window_observation_version: Option<u8>,
    platform: &'static str,
    displays: Vec<DisplayInfo>,
    supported_modes: Vec<&'static str>,
    delivery_policy_version: u8,
    strict_background_input_available: bool,
    strict_background_reason: &'static str,
    reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq, Hash)]
#[serde(deny_unknown_fields)]
pub struct AxOwner {
    host_id: String,
    connection_id: String,
    session_id: String,
    agent_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecuteRequest {
    #[serde(default)]
    mode: Option<ComputerUseMode>,
    #[serde(default)]
    target_scope: Option<TargetScope>,
    #[serde(default)]
    delivery_policy: DeliveryPolicy,
    #[serde(default)]
    owner: Option<AxOwner>,
    action: ComputerAction,
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ComputerUseMode {
    BackgroundApp,
    ForegroundDesktop,
}

impl Default for ComputerUseMode {
    fn default() -> Self {
        Self::BackgroundApp
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComputerAction {
    action: String,
    x: Option<i32>,
    y: Option<i32>,
    to_x: Option<i32>,
    to_y: Option<i32>,
    button: Option<String>,
    delta_x: Option<i32>,
    delta_y: Option<i32>,
    text: Option<String>,
    keys: Option<Vec<String>>,
    display_id: Option<String>,
    window_id: Option<String>,
    duration_ms: Option<u64>,
    include_screenshot: Option<bool>,
    // Internal Core/host negotiation; not a model-facing action argument.
    // Read on macOS. Kept on every platform so deny_unknown_fields still accepts it.
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    include_window_observations: Option<bool>,
    observation: Option<String>,
    snapshot_id: Option<String>,
    element_id: Option<String>,
    ax_action: Option<String>,
}

fn display_info(monitor: &Monitor) -> Result<DisplayInfo, String> {
    Ok(DisplayInfo {
        id: monitor.id().map_err(|error| error.to_string())?.to_string(),
        name: monitor
            .friendly_name()
            .or_else(|_| monitor.name())
            .map_err(|error| error.to_string())?,
        x: monitor.x().map_err(|error| error.to_string())?,
        y: monitor.y().map_err(|error| error.to_string())?,
        width: monitor.width().map_err(|error| error.to_string())?,
        height: monitor.height().map_err(|error| error.to_string())?,
        primary: monitor.is_primary().map_err(|error| error.to_string())?,
    })
}

fn monitors() -> Result<Vec<(Monitor, DisplayInfo)>, String> {
    Monitor::all()
        .map_err(|error| error.to_string())?
        .into_iter()
        .map(|monitor| display_info(&monitor).map(|info| (monitor, info)))
        .collect()
}

fn capability_status(
    capture_error: Option<String>,
    input_error: Option<String>,
) -> (bool, bool, Option<String>) {
    let gui_available = capture_error.is_none();
    let input_available = input_error.is_none();
    let reason = capture_error.or(input_error);
    (gui_available, input_available, reason)
}

pub(crate) fn detect_capabilities() -> ComputerUseCapabilities {
    let found = monitors();
    let displays = found
        .as_ref()
        .map(|items| items.iter().map(|(_, info)| info.clone()).collect())
        .unwrap_or_default();
    let capture_error = match &found {
        Ok(items) => match items
            .iter()
            .find(|(_, info)| info.primary)
            .or_else(|| items.first())
        {
            Some((monitor, _)) => monitor
                .capture_image()
                .err()
                .map(|error| format!("Screen capture is unavailable: {error}")),
            None => Some("No graphical displays were detected".to_string()),
        },
        Err(reason) => Some(reason.clone()),
    };
    let input_error = Enigo::new(&Settings::default())
        .err()
        .map(|error| format!("Desktop input permission is unavailable: {error}"));
    let (capture_available, input_available, reason) =
        capability_status(capture_error, input_error);
    #[cfg(target_os = "macos")]
    let ax_available = mac_accessibility::available();
    #[cfg(not(target_os = "macos"))]
    let ax_available = false;
    let mut modes = supported_modes();
    if !capture_available {
        modes.retain(|mode| *mode != "foreground_desktop");
    }
    ComputerUseCapabilities {
        gui_available: capture_available || ax_available,
        input_available: input_available || ax_available,
        capture_available,
        ax_available,
        ax_protocol_version: cfg!(target_os = "macos").then_some(1),
        window_observation_version: cfg!(target_os = "macos").then_some(1),
        platform: std::env::consts::OS,
        displays,
        supported_modes: modes,
        delivery_policy_version: 1,
        strict_background_input_available: strict_background_input_available(),
        strict_background_reason: strict_background_reason(),
        reason,
    }
}

#[tauri::command]
pub async fn computer_use_capabilities() -> ComputerUseCapabilities {
    tauri::async_runtime::spawn_blocking(detect_capabilities)
        .await
        .unwrap_or_else(|error| ComputerUseCapabilities {
            gui_available: false,
            input_available: false,
            capture_available: false,
            ax_available: false,
            ax_protocol_version: cfg!(target_os = "macos").then_some(1),
            window_observation_version: cfg!(target_os = "macos").then_some(1),
            platform: std::env::consts::OS,
            displays: Vec::new(),
            supported_modes: supported_modes(),
            delivery_policy_version: 1,
            strict_background_input_available: strict_background_input_available(),
            strict_background_reason: strict_background_reason(),
            reason: Some(format!("Computer Use capability detection failed: {error}")),
        })
}

fn supported_modes() -> Vec<&'static str> {
    #[cfg(target_os = "macos")]
    {
        vec!["background_app", "foreground_desktop"]
    }
    #[cfg(not(target_os = "macos"))]
    {
        vec!["foreground_desktop"]
    }
}

#[tauri::command]
pub async fn computer_use_open_input_settings() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        return tauri::async_runtime::spawn_blocking(|| {
            Command::new("open")
                .arg(
                    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                )
                .status()
                .map_err(|error| format!("Unable to open Accessibility settings: {error}"))
                .and_then(|status| {
                    if status.success() {
                        Ok(())
                    } else {
                        Err(format!(
                            "Unable to open Accessibility settings; process exited with {status}"
                        ))
                    }
                })
        })
        .await
        .map_err(|error| format!("Unable to open Accessibility settings: {error}"))?;
    }

    #[cfg(not(target_os = "macos"))]
    Err("Opening Accessibility settings is only supported on macOS".to_string())
}

fn required<T: Copy>(value: Option<T>, name: &str) -> Result<T, String> {
    value.ok_or_else(|| format!("{name} is required"))
}

fn mouse_button(name: Option<&str>) -> Result<Button, String> {
    match name.unwrap_or("left").to_ascii_lowercase().as_str() {
        "left" => Ok(Button::Left),
        "middle" => Ok(Button::Middle),
        "right" => Ok(Button::Right),
        value => Err(format!("Unknown mouse button: {value}")),
    }
}

fn key_from_name(name: &str) -> Result<Key, String> {
    let upper = name.trim().to_ascii_uppercase();
    Ok(match upper.as_str() {
        "ALT" | "OPTION" => Key::Alt,
        "BACKSPACE" => Key::Backspace,
        "CAPSLOCK" | "CAPS_LOCK" => Key::CapsLock,
        "CMD" | "COMMAND" | "META" | "WIN" | "WINDOWS" => Key::Meta,
        "CTRL" | "CONTROL" => Key::Control,
        "DELETE" | "DEL" => Key::Delete,
        "DOWN" | "ARROWDOWN" => Key::DownArrow,
        "END" => Key::End,
        "ENTER" | "RETURN" => Key::Return,
        "ESC" | "ESCAPE" => Key::Escape,
        "HOME" => Key::Home,
        "LEFT" | "ARROWLEFT" => Key::LeftArrow,
        "PAGEDOWN" | "PAGE_DOWN" => Key::PageDown,
        "PAGEUP" | "PAGE_UP" => Key::PageUp,
        "RIGHT" | "ARROWRIGHT" => Key::RightArrow,
        "SHIFT" => Key::Shift,
        "SPACE" => Key::Space,
        "TAB" => Key::Tab,
        "UP" | "ARROWUP" => Key::UpArrow,
        "F1" => Key::F1,
        "F2" => Key::F2,
        "F3" => Key::F3,
        "F4" => Key::F4,
        "F5" => Key::F5,
        "F6" => Key::F6,
        "F7" => Key::F7,
        "F8" => Key::F8,
        "F9" => Key::F9,
        "F10" => Key::F10,
        "F11" => Key::F11,
        "F12" => Key::F12,
        _ => {
            let mut chars = name.chars();
            let character = chars
                .next()
                .ok_or_else(|| "Key cannot be empty".to_string())?;
            if chars.next().is_some() {
                return Err(format!("Unknown key: {name}"));
            }
            Key::Unicode(character)
        }
    })
}

fn press_keys(enigo: &mut Enigo, names: &[String]) -> Result<(), String> {
    if names.is_empty() {
        return Err("keys cannot be empty".to_string());
    }
    let keys = names
        .iter()
        .map(|name| key_from_name(name))
        .collect::<Result<Vec<_>, _>>()?;
    let mut pressed = Vec::with_capacity(keys.len());
    for key in &keys {
        if let Err(error) = enigo.key(*key, Direction::Press) {
            for pressed_key in pressed.iter().rev() {
                let _ = enigo.key(*pressed_key, Direction::Release);
            }
            return Err(error.to_string());
        }
        pressed.push(*key);
    }
    let mut release_error = None;
    for key in pressed.iter().rev() {
        if let Err(error) = enigo.key(*key, Direction::Release) {
            release_error.get_or_insert_with(|| error.to_string());
        }
    }
    if let Some(error) = release_error {
        return Err(error);
    }
    Ok(())
}

fn activate_app(name: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let status = Command::new("open").args(["-a", name]).status();

    #[cfg(target_os = "windows")]
    let status = Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            "Start-Process -FilePath $args[0]",
            name,
        ])
        .creation_flags(0x08000000)
        .status();

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let status = Command::new(name).status();

    match status {
        Ok(value) if value.success() => Ok(()),
        Ok(value) => Err(format!("Unable to open app; process exited with {value}")),
        Err(error) => Err(format!("Unable to open app: {error}")),
    }
}

#[cfg(not(target_os = "windows"))]
fn focus_app(name: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let status = Command::new("osascript")
        .args([
            "-e",
            "on run argv\nset appName to item 1 of argv\ntell application \"System Events\" to set frontmost of process appName to true\nend run",
            name,
        ])
        .status();

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    return Err("Window focus is currently supported on macOS and Windows".to_string());

    #[cfg(any(target_os = "macos", target_os = "windows"))]
    match status {
        Ok(value) if value.success() => Ok(()),
        Ok(value) => Err(format!("Unable to focus app; process exited with {value}")),
        Err(error) => Err(format!("Unable to focus app: {error}")),
    }
}

#[cfg(target_os = "windows")]
fn select_focus_window(
    windows: &[Value],
    id: Option<&str>,
    text: Option<&str>,
) -> Result<u32, String> {
    let matches: Vec<_> = windows
        .iter()
        .filter(|window| {
            if let Some(id) = id {
                window["id"].as_str() == Some(id)
            } else if let Some(text) = text.filter(|text| !text.trim().is_empty()) {
                window["title"].as_str() == Some(text) || window["app_name"].as_str() == Some(text)
            } else {
                false
            }
        })
        .collect();
    match matches.as_slice() {
        [] => Err("Window not found; use list_windows and specify window_id".to_string()),
        [window] => window["id"]
            .as_str()
            .and_then(|id| id.parse().ok())
            .ok_or_else(|| "Invalid window_id".to_string()),
        _ => Err("Multiple windows match; specify window_id from list_windows".to_string()),
    }
}

#[cfg(not(target_os = "macos"))]
fn window_list() -> Result<Vec<Value>, String> {
    Window::all()
        .map_err(|error| error.to_string())?
        .iter()
        .map(|window| {
            Ok(json!({
                "id": window.id().map_err(|error| error.to_string())?.to_string(),
                "pid": window.pid().map_err(|error| error.to_string())?,
                "app_name": window.app_name().map_err(|error| error.to_string())?,
                "title": window.title().map_err(|error| error.to_string())?,
                "x": window.x().map_err(|error| error.to_string())?,
                "y": window.y().map_err(|error| error.to_string())?,
                "width": window.width().map_err(|error| error.to_string())?,
                "height": window.height().map_err(|error| error.to_string())?,
                "focused": window.is_focused().map_err(|error| error.to_string())?,
            }))
        })
        .collect()
}

#[cfg(target_os = "macos")]
fn window_list() -> Result<Vec<Value>, String> {
    // xcap's macOS getters each re-enumerate the entire WindowServer list,
    // and its is_focused() means foreground *process*, not focused window.
    let _timing = diagnostics::Stage::new("window_list");
    let windows = mac_all_window_info()?;
    let front = mac_front_process_serial_number().ok();
    let mut pids = std::collections::BTreeSet::new();
    let front_pid = windows.iter().filter(|w| w.on_screen).find_map(|w| {
        (pids.insert(w.target.pid)
            && front.is_some()
            && mac_process_serial_number(w.target.pid).ok() == front)
            .then_some(w.target.pid)
    });
    let focused = front_pid.and_then(|pid| {
        let application = mac_ax_relations::application(pid).ok()?;
        let window = mac_ax_copy_attribute(&application, "AXFocusedWindow").ok()?;
        mac_ax_window_id(&window).ok()
    });
    Ok(mac_window_list_values(&windows, front_pid, focused))
}

#[cfg(target_os = "macos")]
fn mac_window_list_values(
    windows: &[MacWindowInfo],
    front_pid: Option<i32>,
    focused: Option<u32>,
) -> Vec<Value> {
    windows
        .iter()
        .filter(|w| w.on_screen)
        .map(|w| {
            let foreground = front_pid.map(|pid| pid == w.target.pid);
            let focused = match foreground {
                Some(false) => Some(false),
                Some(true) => focused.map(|id| id == w.target.window_id),
                None => None,
            };
            json!({
                "id": w.target.window_id.to_string(), "pid": w.target.pid,
                "app_name": w.app_name, "title": w.title,
                "x": w.target.x, "y": w.target.y,
                "width": w.target.width, "height": w.target.height,
                "focused": focused, "application_frontmost": foreground,
            })
        })
        .collect()
}

#[cfg(any(target_os = "macos", test))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct WindowTarget {
    window_id: u32,
    pid: i32,
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

#[cfg(target_os = "macos")]
#[derive(Debug, Clone, PartialEq, Eq)]
struct MacWindowInfo {
    target: WindowTarget,
    app_name: String,
    title: String,
    layer: i32,
    on_screen: bool,
}

#[cfg(target_os = "macos")]
#[derive(Debug, PartialEq, Eq)]
struct MacWindowGroup {
    // Components are ordered front-to-back and always include the root.
    components: Vec<MacWindowInfo>,
    relations: WindowRelations,
    // Overlapping surfaces without proven ownership are reported separately.
    // Ordinary sibling documents never become components or input targets.
    excluded: Vec<MacWindowInfo>,
}

#[cfg(target_os = "macos")]
fn mac_dictionary_value(dictionary: &CFDictionary, key: &str) -> Option<CFType> {
    let key = CFString::new(key);
    let value = unsafe {
        CFDictionaryGetValue(dictionary.as_concrete_TypeRef(), key.as_CFTypeRef().cast())
    };
    if value.is_null() {
        None
    } else {
        Some(unsafe { CFType::wrap_under_get_rule(value.cast()) })
    }
}

#[cfg(target_os = "macos")]
fn mac_dictionary_number(dictionary: &CFDictionary, key: &str) -> Option<i64> {
    mac_dictionary_value(dictionary, key)?
        .downcast::<CFNumber>()?
        .to_i64()
}

#[cfg(target_os = "macos")]
fn mac_dictionary_string(dictionary: &CFDictionary, key: &str) -> Option<String> {
    mac_dictionary_value(dictionary, key)
        .and_then(|value| value.downcast::<CFString>())
        .map(|value| value.to_string())
}

#[cfg(target_os = "macos")]
fn mac_dictionary_bool(dictionary: &CFDictionary, key: &str) -> Option<bool> {
    mac_dictionary_value(dictionary, key)
        .and_then(|value| value.downcast::<CFBoolean>())
        .map(|value| value == CFBoolean::true_value())
}

#[cfg(target_os = "macos")]
fn mac_all_window_info() -> Result<Vec<MacWindowInfo>, String> {
    let _timing = diagnostics::Stage::new("window_server_snapshot");
    let windows = copy_window_info(
        kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )
    .ok_or_else(|| "Unable to enumerate macOS windows".to_string())?;
    let mut result = Vec::new();
    for raw in windows.get_all_values() {
        if raw.is_null() {
            continue;
        }
        let dictionary = unsafe {
            CFDictionary::wrap_under_get_rule(raw.cast::<std::ffi::c_void>() as CFDictionaryRef)
        };
        let Some(window_id) = mac_dictionary_number(&dictionary, "kCGWindowNumber")
            .and_then(|value| u32::try_from(value).ok())
        else {
            continue;
        };
        let Some(pid) = mac_dictionary_number(&dictionary, "kCGWindowOwnerPID")
            .and_then(|value| i32::try_from(value).ok())
        else {
            continue;
        };
        if mac_dictionary_number(&dictionary, "kCGWindowSharingState") == Some(0) {
            continue;
        }
        let Some(bounds) = mac_dictionary_value(&dictionary, "kCGWindowBounds")
            .and_then(|value| value.downcast::<CFDictionary>())
            .and_then(|value| CGRect::from_dict_representation(&value))
        else {
            continue;
        };
        if bounds.size.width <= 0.0 || bounds.size.height <= 0.0 {
            continue;
        }
        result.push(MacWindowInfo {
            target: WindowTarget {
                window_id,
                pid,
                x: bounds.origin.x.round() as i32,
                y: bounds.origin.y.round() as i32,
                width: bounds.size.width.round() as u32,
                height: bounds.size.height.round() as u32,
            },
            app_name: mac_dictionary_string(&dictionary, "kCGWindowOwnerName").unwrap_or_default(),
            title: mac_dictionary_string(&dictionary, "kCGWindowName").unwrap_or_default(),
            layer: mac_dictionary_number(&dictionary, "kCGWindowLayer")
                .and_then(|value| i32::try_from(value).ok())
                .unwrap_or_default(),
            on_screen: mac_dictionary_bool(&dictionary, "kCGWindowIsOnscreen").unwrap_or(false),
        });
    }
    Ok(result)
}

#[cfg(target_os = "macos")]
fn window_intersection_area(left: WindowTarget, right: WindowTarget) -> u64 {
    let left_edge = left.x.max(right.x) as i64;
    let top_edge = left.y.max(right.y) as i64;
    let right_edge = (i64::from(left.x) + i64::from(left.width))
        .min(i64::from(right.x) + i64::from(right.width));
    let bottom_edge = (i64::from(left.y) + i64::from(left.height))
        .min(i64::from(right.y) + i64::from(right.height));
    if right_edge <= left_edge || bottom_edge <= top_edge {
        0
    } else {
        (right_edge - left_edge) as u64 * (bottom_edge - top_edge) as u64
    }
}

#[cfg(target_os = "macos")]
fn mac_window_group_from_info(
    root: WindowTarget,
    windows: &[MacWindowInfo],
    relations: &WindowRelations,
) -> MacWindowGroup {
    let root_index = windows
        .iter()
        .position(|window| window.target.window_id == root.window_id);
    let fallback = MacWindowInfo {
        target: root,
        app_name: String::new(),
        title: String::new(),
        layer: 0,
        on_screen: false,
    };
    let root_info = root_index.map(|i| windows[i].clone()).unwrap_or(fallback);
    let root = root_info.target;
    let mut components = Vec::new();
    let mut excluded = Vec::new();
    for window in root_index.map(|i| &windows[..i]).unwrap_or_default() {
        if window.target.pid != root.pid
            || !window.on_screen
            || window_intersection_area(window.target, root) == 0
        {
            continue;
        }
        if relations.belongs_to(window.target.window_id, root.window_id) {
            components.push(window.clone());
        } else {
            excluded.push(window.clone());
        }
    }
    components.push(root_info);
    MacWindowGroup {
        components,
        relations: relations.clone(),
        excluded,
    }
}

#[cfg(target_os = "macos")]
fn mac_window_group(root: WindowTarget) -> Result<MacWindowGroup, String> {
    let _timing = diagnostics::Stage::new("capture_layout");
    let windows = mac_all_window_info()?;
    if !windows
        .iter()
        .any(|window| window.target.window_id == root.window_id && window.target.pid == root.pid)
    {
        return Err(format!(
            "Window not found or changed identity: {}",
            root.window_id
        ));
    }
    let relations = mac_ax_relations::application(root.pid)
        .map(|application| mac_ax_relations::snapshot(&application, root.pid).relations)
        .unwrap_or_default();
    Ok(mac_window_group_from_info(root, &windows, &relations))
}

#[cfg(target_os = "macos")]
fn point_in_window(target: WindowTarget, point: (i32, i32)) -> bool {
    i64::from(point.0) >= i64::from(target.x)
        && i64::from(point.1) >= i64::from(target.y)
        && i64::from(point.0) < i64::from(target.x) + i64::from(target.width)
        && i64::from(point.1) < i64::from(target.y) + i64::from(target.height)
}

#[cfg(target_os = "macos")]
fn mac_event_target_from_group(
    group: MacWindowGroup,
    root: WindowTarget,
    point: (i32, i32),
) -> Result<MacWindowInfo, String> {
    // A sibling document is a separate explicit target. An unclassified
    // overlapping surface could be an editor/dialog: never click through it.
    if let Some(window) = group.excluded.iter().find(|window| {
        group.relations.kind(window.target.window_id) != WindowKind::Document
            && point_in_window(window.target, point)
    }) {
        return Err(format!("Window relationship is unresolved for overlapping window {}; no input was sent. Observe and explicitly select the intended window", window.target.window_id));
    }
    group
        .components
        .into_iter()
        .find(|window| {
            group.relations.kind(window.target.window_id) != WindowKind::Passive
                && point_in_window(window.target, point)
        })
        .ok_or_else(|| {
            format!(
                "No confirmed input surface at the point in window {}",
                root.window_id
            )
        })
}

#[cfg(target_os = "macos")]
fn mac_background_event_target(
    root: WindowTarget,
    point: (i32, i32),
) -> Result<MacWindowInfo, String> {
    mac_event_target_from_group(mac_window_group(root)?, root, point)
}

#[cfg(target_os = "macos")]
fn unresolved_pointer_target(action: &ComputerAction, reason: String) -> Value {
    json!({"ok": false, "action": action.action, "error_code": "window_relationship_unresolved",
        "error": reason, "summary": "Pointer target is unresolved; no input was sent",
        "action_dispatched": false, "dispatch_succeeded": false, "effect_verified": false,
        "requires_observation": true, "retry_safe": true, "focus_changed_by_tool": false})
}

#[cfg(target_os = "macos")]
fn window_target(window_id: &str) -> Result<WindowTarget, String> {
    let id: u32 = window_id.parse().map_err(|_| "Invalid window ID")?;
    mac_all_window_info()?
        .into_iter()
        .find(|window| window.target.window_id == id && window.on_screen)
        .map(|window| window.target)
        .ok_or_else(|| format!("Window not found: {window_id}"))
}

#[cfg(any(target_os = "macos", test))]
fn background_local_point(
    action: &ComputerAction,
    target: WindowTarget,
) -> Result<(i32, i32), String> {
    let x = required(action.x, "x")?;
    let y = required(action.y, "y")?;
    if x < 0 || y < 0 || x as u32 >= target.width || y as u32 >= target.height {
        return Err(format!(
            "Point ({x}, {y}) is outside target window-local bounds 0,0 {}x{}",
            target.width, target.height
        ));
    }
    Ok((x, y))
}

#[cfg(any(target_os = "macos", test))]
fn background_point(action: &ComputerAction, target: WindowTarget) -> Result<(i32, i32), String> {
    let (x, y) = background_local_point(action, target)?;
    Ok((
        target
            .x
            .checked_add(x)
            .ok_or_else(|| "Background X coordinate overflowed desktop space".to_string())?,
        target
            .y
            .checked_add(y)
            .ok_or_else(|| "Background Y coordinate overflowed desktop space".to_string())?,
    ))
}

#[cfg(target_os = "macos")]
fn mac_event_source() -> Result<CGEventSource, String> {
    CGEventSource::new(CGEventSourceStateID::Private)
        .map_err(|_| "Unable to create a private macOS input source".to_string())
}

#[cfg(target_os = "macos")]
fn mac_require_input_permission() -> Result<(), String> {
    let settings = Settings {
        open_prompt_to_get_permissions: false,
        ..Settings::default()
    };
    Enigo::new(&settings)
        .map(|_| ())
        .map_err(|error| format!("macOS Accessibility permission is required: {error}"))
}

#[cfg(target_os = "macos")]
type AXError = i32;

#[cfg(target_os = "macos")]
const AX_ERROR_SUCCESS: AXError = 0;
#[cfg(target_os = "macos")]
const AX_ERROR_CANNOT_COMPLETE: AXError = -25204;
#[cfg(target_os = "macos")]
const AX_ERROR_ACTION_UNSUPPORTED: AXError = -25206;

#[cfg(target_os = "macos")]
#[link(name = "ApplicationServices", kind = "framework")]
unsafe extern "C" {
    fn AXUIElementCreateApplication(pid: libc::pid_t) -> CFTypeRef;
    fn AXUIElementCopyElementAtPosition(
        application: CFTypeRef,
        x: f32,
        y: f32,
        element: *mut CFTypeRef,
    ) -> AXError;
    fn AXUIElementCopyAttributeValue(
        element: CFTypeRef,
        attribute: CFStringRef,
        value: *mut CFTypeRef,
    ) -> AXError;
    fn AXUIElementCopyActionNames(element: CFTypeRef, actions: *mut CFArrayRef) -> AXError;
    fn AXUIElementSetAttributeValue(
        element: CFTypeRef,
        attribute: CFStringRef,
        value: CFTypeRef,
    ) -> AXError;
    fn AXUIElementPerformAction(element: CFTypeRef, action: CFStringRef) -> AXError;
    fn AXUIElementGetPid(element: CFTypeRef, pid: *mut libc::pid_t) -> AXError;
    fn AXUIElementSetMessagingTimeout(element: CFTypeRef, seconds: f32) -> AXError;
    fn AXValueGetType(value: CFTypeRef) -> u32;
    fn AXValueGetTypeID() -> usize;
    fn AXValueGetValue(value: CFTypeRef, value_type: u32, output: *mut libc::c_void) -> bool;
}

#[cfg(target_os = "macos")]
#[derive(Debug)]
enum MacAxPressOutcome {
    Performed { action: &'static str },
    Unsupported { reason: String },
    Uncertain { reason: String },
}

#[cfg(target_os = "macos")]
fn mac_ax_error(operation: &str, status: AXError) -> String {
    format!("{operation} failed with AXError {status}")
}

#[cfg(target_os = "macos")]
fn mac_ax_limit_message(element: &CFType) {
    // This is a client-local timeout, not a focus/input mutation. It must be
    // applied to each element: setting it on an application does not propagate
    // to its children (AXUIElementSetMessagingTimeout's documented contract).
    unsafe {
        AXUIElementSetMessagingTimeout(element.as_CFTypeRef(), 1.0);
    }
}

#[cfg(target_os = "macos")]
fn mac_ax_copy_attribute(element: &CFType, attribute: &str) -> Result<CFType, AXError> {
    mac_ax_limit_message(element);
    let attribute = CFString::new(attribute);
    let mut value_ref: CFTypeRef = std::ptr::null();
    let status = unsafe {
        AXUIElementCopyAttributeValue(
            element.as_CFTypeRef(),
            attribute.as_concrete_TypeRef(),
            &mut value_ref,
        )
    };
    if status != AX_ERROR_SUCCESS || value_ref.is_null() {
        return Err(status);
    }
    Ok(unsafe { CFType::wrap_under_create_rule(value_ref) })
}

#[cfg(target_os = "macos")]
fn mac_ax_supports_action(element: &CFType, action: &str) -> Result<bool, AXError> {
    mac_ax_limit_message(element);
    let mut actions_ref: CFArrayRef = std::ptr::null();
    let status = unsafe { AXUIElementCopyActionNames(element.as_CFTypeRef(), &mut actions_ref) };
    if status != AX_ERROR_SUCCESS || actions_ref.is_null() {
        return Err(status);
    }
    let actions = unsafe { CFArray::<CFString>::wrap_under_create_rule(actions_ref) };
    Ok(actions
        .iter()
        .any(|candidate| candidate.to_string() == action))
}

#[cfg(target_os = "macos")]
fn mac_ax_window_id(element: &CFType) -> Result<u32, String> {
    mac_ax_limit_message(element);
    type GetWindow = unsafe extern "C" fn(CFTypeRef, *mut u32) -> AXError;
    static GET_WINDOW: std::sync::OnceLock<Option<GetWindow>> = std::sync::OnceLock::new();
    let get_window = GET_WINDOW.get_or_init(|| {
        for name in [c"_AXUIElementGetWindow", c"AXUIElementGetWindow"] {
            let symbol = unsafe { libc::dlsym(libc::RTLD_DEFAULT, name.as_ptr()) };
            if !symbol.is_null() {
                return Some(unsafe {
                    std::mem::transmute::<*mut libc::c_void, GetWindow>(symbol)
                });
            }
        }
        None
    });
    if let Some(get_window) = get_window {
        let mut window_id = 0;
        let status = unsafe { get_window(element.as_CFTypeRef(), &mut window_id) };
        if status == AX_ERROR_SUCCESS && window_id != 0 {
            return Ok(window_id);
        }
    }

    let window = mac_ax_copy_attribute(element, "AXWindow")
        .map_err(|status| mac_ax_error("Reading AXWindow", status))?;
    // AXWindowNumber is supplied by the macOS accessibility server. Matching
    // it prevents a hit test from acting on another overlapping window owned
    // by the same Electron process.
    let number = mac_ax_copy_attribute(&window, "AXWindowNumber")
        .map_err(|status| mac_ax_error("Reading AXWindowNumber", status))?
        .downcast::<CFNumber>()
        .ok_or_else(|| "AXWindowNumber was not numeric".to_string())?;
    let value = number
        .to_i64()
        .ok_or_else(|| "AXWindowNumber was outside the supported range".to_string())?;
    u32::try_from(value).map_err(|_| "AXWindowNumber was outside the supported range".to_string())
}

#[cfg(target_os = "macos")]
fn mac_ax_frame(element: &CFType) -> Option<CGRect> {
    let position = mac_ax_copy_attribute(element, "AXPosition").ok()?;
    let size = mac_ax_copy_attribute(element, "AXSize").ok()?;
    let mut origin = CGPoint::new(0.0, 0.0);
    let mut dimensions = CGSize::new(0.0, 0.0);
    // AXValue's public CGPoint/CGSize type IDs are 1 and 2 respectively.
    if position.type_of() != unsafe { AXValueGetTypeID() }
        || size.type_of() != unsafe { AXValueGetTypeID() }
        || unsafe { AXValueGetType(position.as_CFTypeRef()) } != 1
        || unsafe { AXValueGetType(size.as_CFTypeRef()) } != 2
        || !unsafe {
            AXValueGetValue(
                position.as_CFTypeRef(),
                1,
                (&mut origin as *mut CGPoint).cast(),
            )
        }
        || !unsafe {
            AXValueGetValue(
                size.as_CFTypeRef(),
                2,
                (&mut dimensions as *mut CGSize).cast(),
            )
        }
    {
        return None;
    }
    if ![origin.x, origin.y, dimensions.width, dimensions.height]
        .into_iter()
        .all(f64::is_finite)
    {
        return None;
    }
    Some(CGRect::new(&origin, &dimensions))
}

#[cfg(target_os = "macos")]
fn mac_ax_contains_point(element: &CFType, point: CGPoint) -> Option<bool> {
    let frame = mac_ax_frame(element)?;
    Some(frame.size.width > 0.0 && frame.size.height > 0.0 && frame.contains(&point))
}

#[cfg(target_os = "macos")]
#[derive(Debug, Clone, Copy)]
struct MacAxCoordinateTransform {
    desktop_origin: CGPoint,
    ax_origin: CGPoint,
    scale_x: f64,
    scale_y: f64,
}

#[cfg(target_os = "macos")]
impl MacAxCoordinateTransform {
    fn from_window_content(target: WindowTarget, content: CGRect) -> Option<Self> {
        if target.width == 0 || target.height == 0 {
            return None;
        }
        let scale_x = content.size.width / f64::from(target.width);
        let scale_y = content.size.height / f64::from(target.height);
        if ![scale_x, scale_y, content.origin.x, content.origin.y]
            .into_iter()
            .all(f64::is_finite)
            || scale_x <= 0.0
            || scale_y <= 0.0
            || (scale_x - 1.0).abs() < 0.01
            || (scale_y - 1.0).abs() < 0.01
            || (content.size.height - f64::from(target.height) * scale_x).abs() > 2.0
            || (content.size.width - f64::from(target.width) * scale_y).abs() > 2.0
            || (content.origin.x - f64::from(target.x) * scale_x).abs() > 2.0
            || (content.origin.y - f64::from(target.y) * scale_y).abs() > 2.0
        {
            return None;
        }
        Some(Self {
            desktop_origin: CGPoint::new(f64::from(target.x), f64::from(target.y)),
            ax_origin: content.origin,
            scale_x,
            scale_y,
        })
    }

    fn point(self, point: CGPoint) -> CGPoint {
        CGPoint::new(
            self.ax_origin.x + (point.x - self.desktop_origin.x) * self.scale_x,
            self.ax_origin.y + (point.y - self.desktop_origin.y) * self.scale_y,
        )
    }
}

#[cfg(target_os = "macos")]
struct MacAxScaledContent {
    root: CFType,
    transform: MacAxCoordinateTransform,
}

#[cfg(target_os = "macos")]
fn mac_ax_string(element: &CFType, attribute: &str) -> Option<String> {
    mac_ax_copy_attribute(element, attribute)
        .ok()?
        .downcast::<CFString>()
        .map(|value| value.to_string())
}

#[cfg(target_os = "macos")]
fn mac_ax_children(element: &CFType) -> Vec<CFType> {
    mac_ax_copy_attribute(element, "AXChildren")
        .ok()
        .and_then(|value| value.downcast::<CFArray>())
        .map(|children| {
            children
                .get_all_values()
                .into_iter()
                .filter(|child| !child.is_null())
                .map(|child| unsafe { CFType::wrap_under_get_rule(child.cast()) })
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(target_os = "macos")]
fn mac_ax_scaled_content(window: &CFType, target: WindowTarget) -> Option<MacAxScaledContent> {
    for root in mac_ax_children(window) {
        if mac_ax_string(&root, "AXRole").as_deref() != Some("AXGroup")
            || mac_ax_window_id(&root) != Ok(target.window_id)
        {
            continue;
        }
        let Some(frame) = mac_ax_frame(&root) else {
            continue;
        };
        let Some(transform) = MacAxCoordinateTransform::from_window_content(target, frame) else {
            continue;
        };
        // Recognize Chromium's full-window content container, not arbitrary
        // scaled panels, images or scroll documents. Native title-bar buttons
        // are siblings of this root and must keep their desktop coordinates.
        let contents_view = mac_ax_children(&root).into_iter().any(|child| {
            mac_ax_string(&child, "AXDescription").as_deref() == Some("ContentsView")
                && mac_ax_window_id(&child) == Ok(target.window_id)
                && mac_ax_frame(&child)
                    .map(|child_frame| {
                        child_frame.origin.x == frame.origin.x
                            && child_frame.origin.y == frame.origin.y
                            && child_frame.size.width == frame.size.width
                            && child_frame.size.height == frame.size.height
                    })
                    .unwrap_or(false)
        });
        if contents_view {
            return Some(MacAxScaledContent { root, transform });
        }
    }
    None
}

#[cfg(target_os = "macos")]
fn mac_ax_pressable_at_point(
    element: CFType,
    target: WindowTarget,
    point: CGPoint,
    depth: usize,
    remaining: &mut usize,
    scaled_content: Option<&MacAxScaledContent>,
    relations: &WindowRelations,
) -> Option<CFType> {
    if depth > 32 || *remaining == 0 {
        return None;
    }
    *remaining -= 1;
    let point = match scaled_content.filter(|content| content.root == element) {
        Some(content) => content.transform.point(point),
        None => point,
    };
    for (attribute, unavailable) in [("AXHidden", true), ("AXEnabled", false)] {
        if mac_ax_copy_attribute(&element, attribute)
            .ok()
            .and_then(|value| value.downcast::<CFBoolean>())
            .map(bool::from)
            == Some(unavailable)
        {
            return None;
        }
    }
    let contains = mac_ax_contains_point(&element, point);
    if contains == Some(false) {
        return None;
    }
    for child in mac_ax_children(&element).into_iter().rev() {
        if let Some(hit) = mac_ax_pressable_at_point(
            child,
            target,
            point,
            depth + 1,
            remaining,
            scaled_content,
            relations,
        ) {
            return Some(hit);
        }
    }
    // Unknown geometry may belong to a container; traverse it but never
    // invoke an action on it. Keep the final candidate in the selected window.
    if contains == Some(true)
        && mac_ax_element_in_target(&element, target, relations)
        && (mac_ax_supports_action(&element, "AXPress") == Ok(true)
            || mac_ax_supports_action(&element, "AXPick") == Ok(true))
    {
        Some(element)
    } else {
        None
    }
}

#[cfg(target_os = "macos")]
fn mac_ax_element_in_target(
    element: &CFType,
    target: WindowTarget,
    relations: &WindowRelations,
) -> bool {
    let mut pid = 0;
    if unsafe { AXUIElementGetPid(element.as_CFTypeRef(), &mut pid) } != AX_ERROR_SUCCESS
        || pid != target.pid
    {
        return false;
    }
    mac_ax_window_id(element).is_ok_and(|id| {
        relations.kind(id) != WindowKind::Passive
            && (id == target.window_id || relations.belongs_to(id, target.window_id))
    })
}

#[cfg(target_os = "macos")]
fn mac_ax_window(application: &CFType, target: WindowTarget) -> Result<CFType, String> {
    let _timing = diagnostics::Stage::new("ax_window_lookup");
    mac_ax_relations::find_window(application, target)
}

#[cfg(all(test, target_os = "macos"))]
fn mac_ax_click_target(
    application: &CFType,
    target: WindowTarget,
    x: i32,
    y: i32,
) -> Result<CFType, String> {
    let relations = mac_ax_relations::snapshot(application, target.pid).relations;
    mac_ax_click_target_in_group(application, target, x, y, &relations)
}

#[cfg(target_os = "macos")]
fn mac_ax_click_target_in_group(
    application: &CFType,
    target: WindowTarget,
    x: i32,
    y: i32,
    relations: &WindowRelations,
) -> Result<CFType, String> {
    let window = mac_ax_window(application, target);
    let scaled_content = window
        .as_ref()
        .ok()
        .and_then(|window| mac_ax_scaled_content(window, target));
    if scaled_content.is_none() {
        let mut hit_ref: CFTypeRef = std::ptr::null();
        let status = unsafe {
            AXUIElementCopyElementAtPosition(
                application.as_CFTypeRef(),
                x as f32,
                y as f32,
                &mut hit_ref,
            )
        };
        if !hit_ref.is_null() {
            let hit = unsafe { CFType::wrap_under_create_rule(hit_ref) };
            // Native save sheets can hit-test to their non-actionable shell,
            // while their buttons live in an AX-owned remote content window.
            // In that case search the sheet tree instead of pressing the shell
            // or prematurely falling back to a mouse event.
            if status == AX_ERROR_SUCCESS
                && mac_ax_element_in_target(&hit, target, relations)
                && (mac_ax_supports_action(&hit, "AXPress") == Ok(true)
                    || mac_ax_supports_action(&hit, "AXPick") == Ok(true))
            {
                return Ok(hit);
            }
        }
    }

    let mut remaining = 1024;
    mac_ax_pressable_at_point(
        window?,
        target,
        CGPoint::new(f64::from(x), f64::from(y)),
        0,
        &mut remaining,
        scaled_content.as_ref(),
        relations,
    )
    .ok_or_else(|| "No background accessibility click action at the target point".to_string())
}

#[cfg(target_os = "macos")]
fn mac_ax_press(target: WindowTarget, x: i32, y: i32) -> MacAxPressOutcome {
    let _timing = diagnostics::Stage::new("ax_click");
    let application_ref = unsafe { AXUIElementCreateApplication(target.pid) };
    if application_ref.is_null() {
        return MacAxPressOutcome::Unsupported {
            reason: "Unable to create the target application's accessibility element".to_string(),
        };
    }
    let application = unsafe { CFType::wrap_under_create_rule(application_ref) };
    mac_ax_limit_message(&application);

    // Chromium/Electron applications may not expose their accessibility tree
    // until an assistive client opts in. This does not activate, focus, or
    // raise the application.
    let manual_accessibility = CFString::new("AXManualAccessibility");
    let enabled = CFBoolean::true_value();
    let manual_status = unsafe {
        AXUIElementSetAttributeValue(
            application.as_CFTypeRef(),
            manual_accessibility.as_concrete_TypeRef(),
            enabled.as_CFTypeRef(),
        )
    };
    if manual_status == AX_ERROR_SUCCESS {
        thread::sleep(Duration::from_millis(50));
    }

    let parent_attribute = CFString::new("AXParent");
    let press_action = CFString::new("AXPress");
    let pick_action = CFString::new("AXPick");
    let relations = mac_ax_relations::snapshot(&application, target.pid).relations;
    let mut current = match mac_ax_click_target_in_group(&application, target, x, y, &relations) {
        Ok(hit) => hit,
        Err(reason) => return MacAxPressOutcome::Unsupported { reason },
    };
    for _ in 0..16 {
        let mut element_pid = 0;
        let pid_status = unsafe { AXUIElementGetPid(current.as_CFTypeRef(), &mut element_pid) };
        if pid_status != AX_ERROR_SUCCESS || element_pid != target.pid {
            return MacAxPressOutcome::Unsupported {
                reason: "Accessibility hit test escaped the target application".to_string(),
            };
        }
        if !mac_ax_element_in_target(&current, target, &relations) {
            break;
        }

        for (name, action) in [("AXPress", &press_action), ("AXPick", &pick_action)] {
            // Some Chromium groups return success for arbitrary AX actions
            // even though their advertised action list contains only
            // AXShowMenu/AXScrollToVisible. Treat those no-op acknowledgements
            // as unsupported. A raw mouse fallback can activate the app.
            if mac_ax_supports_action(&current, name) != Ok(true) {
                continue;
            }
            let status = unsafe {
                AXUIElementPerformAction(current.as_CFTypeRef(), action.as_concrete_TypeRef())
            };
            match status {
                AX_ERROR_SUCCESS => return MacAxPressOutcome::Performed { action: name },
                AX_ERROR_ACTION_UNSUPPORTED => {}
                AX_ERROR_CANNOT_COMPLETE => {
                    // Apple documents that CannotComplete may still mean the
                    // application handled the action. Do not risk a duplicate
                    // click through the Quartz fallback.
                    return MacAxPressOutcome::Uncertain {
                        reason: mac_ax_error(name, status),
                    };
                }
                _ => {
                    return MacAxPressOutcome::Uncertain {
                        reason: mac_ax_error(name, status),
                    };
                }
            }
        }

        let mut parent_ref: CFTypeRef = std::ptr::null();
        let parent_status = unsafe {
            AXUIElementCopyAttributeValue(
                current.as_CFTypeRef(),
                parent_attribute.as_concrete_TypeRef(),
                &mut parent_ref,
            )
        };
        if parent_status != AX_ERROR_SUCCESS || parent_ref.is_null() {
            break;
        }
        current = unsafe { CFType::wrap_under_create_rule(parent_ref) };
    }

    MacAxPressOutcome::Unsupported {
        reason: "No accessibility element at the point supports AXPress or AXPick".to_string(),
    }
}

#[cfg(target_os = "macos")]
fn mac_mouse_button(name: Option<&str>) -> Result<CGMouseButton, String> {
    match name.unwrap_or("left").to_ascii_lowercase().as_str() {
        "left" => Ok(CGMouseButton::Left),
        "middle" => Ok(CGMouseButton::Center),
        "right" => Ok(CGMouseButton::Right),
        value => Err(format!("Unknown mouse button: {value}")),
    }
}

#[cfg(target_os = "macos")]
fn mac_mouse_event_types(button: CGMouseButton) -> (CGEventType, CGEventType, CGEventType) {
    match button {
        CGMouseButton::Left => (
            CGEventType::LeftMouseDown,
            CGEventType::LeftMouseUp,
            CGEventType::LeftMouseDragged,
        ),
        CGMouseButton::Right => (
            CGEventType::RightMouseDown,
            CGEventType::RightMouseUp,
            CGEventType::RightMouseDragged,
        ),
        CGMouseButton::Center => (
            CGEventType::OtherMouseDown,
            CGEventType::OtherMouseUp,
            CGEventType::OtherMouseDragged,
        ),
    }
}

#[cfg(target_os = "macos")]
const CG_EVENT_TARGET_WINDOW: u32 = 51;
#[cfg(target_os = "macos")]
const CG_EVENT_RECEIVING_WINDOW: u32 = 52;

#[cfg(target_os = "macos")]
fn mac_target_pointer_event(event: &CGEvent, target: WindowTarget) -> Result<(), String> {
    type SetWindowLocation = unsafe extern "C" fn(core_graphics::sys::CGEventRef, CGPoint);
    static SET_WINDOW_LOCATION: std::sync::OnceLock<Option<SetWindowLocation>> =
        std::sync::OnceLock::new();
    let set_window_location = SET_WINDOW_LOCATION.get_or_init(|| {
        // This private CoreGraphics symbol preserves the local point when
        // posting to a non-key window. Resolve at runtime so an OS without it
        // can still start the app and use observation/foreground control.
        let symbol =
            unsafe { libc::dlsym(libc::RTLD_DEFAULT, c"CGEventSetWindowLocation".as_ptr()) };
        if symbol.is_null() {
            None
        } else {
            // SAFETY: the symbol has the CGEventRef/CGPoint C ABI above, and
            // CoreGraphics stays loaded for the entire process lifetime.
            Some(unsafe { std::mem::transmute::<*mut libc::c_void, SetWindowLocation>(symbol) })
        }
    });
    let set_window_location = set_window_location.ok_or_else(|| {
        "Window-targeted background input is unavailable on this macOS version".to_string()
    })?;
    // A PID can own multiple overlapping windows. Preserve the selected
    // CGWindowID for AppKit's event routing, including synthetic MouseMoved.
    // The public mouse-only fields 91/92 are ignored on wheel events; slots
    // 51/52 carry the receiving window number for both event types.
    event.set_integer_value_field(CG_EVENT_TARGET_WINDOW, i64::from(target.window_id));
    event.set_integer_value_field(CG_EVENT_RECEIVING_WINDOW, i64::from(target.window_id));
    event.set_integer_value_field(
        EventField::MOUSE_EVENT_WINDOW_UNDER_MOUSE_POINTER,
        i64::from(target.window_id),
    );
    event.set_integer_value_field(
        EventField::MOUSE_EVENT_WINDOW_UNDER_MOUSE_POINTER_THAT_CAN_HANDLE_THIS_EVENT,
        i64::from(target.window_id),
    );
    let global = event.location();
    let local = CGPoint::new(
        global.x - f64::from(target.x),
        global.y - f64::from(target.y),
    );
    // SAFETY: a live, owned CGEvent is passed with a window-local point.
    unsafe { set_window_location(event.as_ptr(), local) };
    if event.get_integer_value_field(CG_EVENT_TARGET_WINDOW) != i64::from(target.window_id) {
        return Err("macOS did not preserve the background event's target window".to_string());
    }
    Ok(())
}

#[cfg(target_os = "macos")]
type MacGetProcessForPid = unsafe extern "C" fn(i32, *mut u32) -> i32;
#[cfg(target_os = "macos")]
type MacGetFrontProcess = unsafe extern "C" fn(*mut u32) -> i32;

#[cfg(target_os = "macos")]
fn mac_process_api() -> Result<(MacGetProcessForPid, MacGetFrontProcess), String> {
    static API: std::sync::OnceLock<Option<(MacGetProcessForPid, MacGetFrontProcess)>> =
        std::sync::OnceLock::new();
    API.get_or_init(|| unsafe {
        let get = libc::dlsym(libc::RTLD_DEFAULT, c"GetProcessForPID".as_ptr());
        let front = libc::dlsym(libc::RTLD_DEFAULT, c"_SLPSGetFrontProcess".as_ptr());
        if get.is_null() || front.is_null() {
            None
        } else {
            Some((
                std::mem::transmute::<*mut libc::c_void, MacGetProcessForPid>(get),
                std::mem::transmute::<*mut libc::c_void, MacGetFrontProcess>(front),
            ))
        }
    })
    .as_ref()
    .copied()
    .ok_or_else(|| "macOS foreground verification is unavailable".to_string())
}

#[cfg(target_os = "macos")]
fn mac_process_serial_number(pid: i32) -> Result<[u32; 2], String> {
    let (get, _) = mac_process_api()?;
    let mut process = [0u32; 2];
    if unsafe { get(pid, process.as_mut_ptr()) } != 0 {
        return Err(format!("Unable to resolve process identity for PID {pid}"));
    }
    Ok(process)
}

#[cfg(target_os = "macos")]
fn mac_front_process_serial_number() -> Result<[u32; 2], String> {
    let (_, front) = mac_process_api()?;
    let mut process = [0u32; 2];
    if unsafe { front(process.as_mut_ptr()) } != 0 {
        return Err("Unable to read the macOS foreground process".to_string());
    }
    Ok(process)
}

#[cfg(target_os = "macos")]
struct MacForegroundMonitor {
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    activated: std::sync::Arc<std::sync::atomic::AtomicBool>,
    verification_failed: std::sync::Arc<std::sync::atomic::AtomicBool>,
    handle: Option<std::thread::JoinHandle<()>>,
}

#[cfg(target_os = "macos")]
impl MacForegroundMonitor {
    fn start(target_pid: i32) -> Result<Self, String> {
        use std::sync::atomic::Ordering;

        let target = mac_process_serial_number(target_pid)?;
        let initial = mac_front_process_serial_number()?;
        let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let activated = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let verification_failed = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let handle = if initial == target {
            None
        } else {
            let thread_stop = stop.clone();
            let thread_activated = activated.clone();
            let thread_failed = verification_failed.clone();
            Some(thread::spawn(move || {
                while !thread_stop.load(Ordering::Acquire) {
                    match mac_front_process_serial_number() {
                        Ok(front) if front == target => {
                            thread_activated.store(true, Ordering::Release);
                        }
                        Ok(_) => {}
                        Err(_) => {
                            thread_failed.store(true, Ordering::Release);
                            break;
                        }
                    }
                    thread::sleep(Duration::from_millis(2));
                }
            }))
        };
        Ok(Self {
            stop,
            activated,
            verification_failed,
            handle,
        })
    }

    fn finish(mut self) -> Result<bool, String> {
        use std::sync::atomic::Ordering;

        self.stop.store(true, Ordering::Release);
        if let Some(handle) = self.handle.take() {
            handle
                .join()
                .map_err(|_| "Foreground verification thread failed".to_string())?;
        }
        if self.verification_failed.load(Ordering::Acquire) {
            return Err("macOS foreground verification failed during input".to_string());
        }
        Ok(self.activated.load(Ordering::Acquire))
    }
}

#[cfg(target_os = "macos")]
impl Drop for MacForegroundMonitor {
    fn drop(&mut self) {
        use std::sync::atomic::Ordering;

        self.stop.store(true, Ordering::Release);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

#[cfg(target_os = "macos")]
fn mac_mouse_event(
    target: WindowTarget,
    event_type: CGEventType,
    button: CGMouseButton,
    x: i32,
    y: i32,
    click_count: i64,
) -> Result<CGEvent, String> {
    let event = CGEvent::new_mouse_event(
        mac_event_source()?,
        event_type,
        CGPoint::new(x as f64, y as f64),
        button,
    )
    .map_err(|_| "Unable to create a macOS mouse event".to_string())?;
    event.set_integer_value_field(EventField::MOUSE_EVENT_CLICK_STATE, click_count);
    mac_target_pointer_event(&event, target)?;
    Ok(event)
}

#[cfg(target_os = "macos")]
fn mac_post_mouse(
    target: WindowTarget,
    event_type: CGEventType,
    button: CGMouseButton,
    x: i32,
    y: i32,
    click_count: i64,
) -> Result<(), String> {
    mac_mouse_event(target, event_type, button, x, y, click_count)?.post_to_pid(target.pid);
    Ok(())
}

#[cfg(target_os = "macos")]
fn mac_post_prepared_mouse(event: &CGEvent, pid: i32) {
    unsafe extern "C" {
        fn CGEventSetTimestamp(event: core_graphics::sys::CGEventRef, timestamp: u64);
        fn clock_gettime_nsec_np(clock_id: libc::clockid_t) -> u64;
    }
    // Prepared drag/double-click events must reflect dispatch time, not their
    // shared construction time. Quartz timestamps use uptime in nanoseconds.
    unsafe {
        CGEventSetTimestamp(
            event.as_ptr(),
            clock_gettime_nsec_np(libc::CLOCK_UPTIME_RAW),
        );
    }
    event.post_to_pid(pid);
}

#[cfg(target_os = "macos")]
fn mac_prepare_mouse_window(target: WindowTarget) -> Result<(), ClickPreparationFailure> {
    let _timing = diagnostics::Stage::new("mouse_preparation");
    let application_ref = unsafe { AXUIElementCreateApplication(target.pid) };
    if application_ref.is_null() {
        return Err(ClickFailureStage::WindowPreparation
            .failure("Unable to prepare the target application for mouse input".to_string()));
    }
    let application = unsafe { CFType::wrap_under_create_rule(application_ref) };
    mac_ax_limit_message(&application);
    let enabled = CFBoolean::true_value();
    // A correctly routed CGEvent can reach an inactive Chromium process and
    // still be discarded before its content sees mouseDown. Application mode
    // allows activation: select the requested window and activate it BEFORE
    // the one intended gesture, rather than retrying a possibly delivered click.
    if let Ok(window) = mac_ax_window(&application, target) {
        let main = CFString::new("AXMain");
        // Panels need not expose AXMain. AXRaise and the event's window number
        // still select them; changing AXMain is useful for ordinary app windows.
        unsafe {
            AXUIElementSetAttributeValue(
                window.as_CFTypeRef(),
                main.as_concrete_TypeRef(),
                enabled.as_CFTypeRef(),
            );
        }
        if mac_ax_supports_action(&window, "AXRaise") == Ok(true) {
            let raise = CFString::new("AXRaise");
            let status = unsafe {
                AXUIElementPerformAction(window.as_CFTypeRef(), raise.as_concrete_TypeRef())
            };
            if status != AX_ERROR_SUCCESS {
                return Err(ClickFailureStage::WindowPreparation.failure(mac_ax_error(
                    "Preparing the selected window for mouse input",
                    status,
                )));
            }
        }
    }
    let frontmost = CFString::new("AXFrontmost");
    let status = unsafe {
        AXUIElementSetAttributeValue(
            application.as_CFTypeRef(),
            frontmost.as_concrete_TypeRef(),
            enabled.as_CFTypeRef(),
        )
    };
    if status != AX_ERROR_SUCCESS {
        return Err(ClickFailureStage::Activation.failure(mac_ax_error(
            "Activating the target for mouse input",
            status,
        )));
    }
    let process = mac_process_serial_number(target.pid)
        .map_err(|reason| ClickFailureStage::Activation.failure(reason))?;
    for _ in 0..25 {
        if mac_front_process_serial_number()
            .map_err(|reason| ClickFailureStage::Activation.failure(reason))?
            == process
        {
            // The application still needs to consume its activation message.
            thread::sleep(Duration::from_millis(50));
            if mac_front_process_serial_number()
                .map_err(|reason| ClickFailureStage::Activation.failure(reason))?
                == process
            {
                return Ok(());
            }
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    Err(ClickFailureStage::Activation.failure(
        "The target did not remain active for mouse input; observe again before clicking"
            .to_string(),
    ))
}

#[cfg(target_os = "macos")]
fn mac_validate_mouse_layout(
    root: WindowTarget,
    event_target: WindowTarget,
    point: (i32, i32),
) -> Result<(), String> {
    let windows = mac_all_window_info()?;
    let stable = [root, event_target].iter().all(|target| {
        windows
            .iter()
            .any(|window| window.target == *target && window.on_screen)
    });
    let application = mac_ax_relations::application(root.pid)?;
    let relations = mac_ax_relations::snapshot(&application, root.pid).relations;
    let group = mac_window_group_from_info(root, &windows, &relations);
    let selected = mac_event_target_from_group(group, root, point)?;
    if !stable || selected.target != event_target || !selected.on_screen {
        return Err(
            "Window layout changed while preparing mouse input; no click was sent. Observe again before clicking".to_string(),
        );
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn mac_click(
    target: WindowTarget,
    x: i32,
    y: i32,
    button_name: Option<&str>,
    click_count: i64,
) -> Result<(), String> {
    let _timing = diagnostics::Stage::new("mouse_dispatch");
    let button = mac_mouse_button(button_name)?;
    let (down, up, _) = mac_mouse_event_types(button);
    // Chromium-based applications use the preceding pointer location for hit
    // testing and hover state. Send a target-local move before mouse-down; the
    // event is posted only to the target PID and does not move the real cursor.
    let moved = mac_mouse_event(
        target,
        CGEventType::MouseMoved,
        CGMouseButton::Left,
        x,
        y,
        0,
    )?;
    // Construct every event before sending mouse-down. The application-mode
    // caller prepares input activation separately using accessibility APIs.
    let events = (1..=click_count)
        .map(|count| {
            Ok((
                mac_mouse_event(target, down, button, x, y, count)?,
                mac_mouse_event(target, up, button, x, y, count)?,
            ))
        })
        .collect::<Result<Vec<_>, String>>()?;
    mac_post_prepared_mouse(&moved, target.pid);
    thread::sleep(Duration::from_millis(12));
    for (index, (down, up)) in events.iter().enumerate() {
        mac_post_prepared_mouse(down, target.pid);
        thread::sleep(Duration::from_millis(20));
        mac_post_prepared_mouse(up, target.pid);
        if index + 1 < events.len() {
            thread::sleep(Duration::from_millis(80));
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn mac_drag(
    target: WindowTarget,
    start: (i32, i32),
    end: (i32, i32),
    button_name: Option<&str>,
    duration_ms: u64,
) -> Result<(), String> {
    let button = mac_mouse_button(button_name)?;
    let (down, up, dragged) = mac_mouse_event_types(button);
    let down = mac_mouse_event(target, down, button, start.0, start.1, 1)?;
    let up = mac_mouse_event(target, up, button, end.0, end.1, 1)?;
    let steps = 20i32;
    let motion = (1..=steps)
        .map(|step| {
            let x = start.0 + (end.0 - start.0) * step / steps;
            let y = start.1 + (end.1 - start.1) * step / steps;
            mac_mouse_event(target, dragged, button, x, y, 1)
        })
        .collect::<Result<Vec<_>, _>>()?;
    mac_post_prepared_mouse(&down, target.pid);
    for event in motion {
        mac_post_prepared_mouse(&event, target.pid);
        thread::sleep(Duration::from_millis(duration_ms / steps as u64));
    }
    mac_post_prepared_mouse(&up, target.pid);
    Ok(())
}

fn scroll_delta(action: &ComputerAction) -> Result<(i32, i32), String> {
    if action.x.is_some() != action.y.is_some() {
        return Err("x and y must be provided together for scroll".to_string());
    }
    let dx = action.delta_x.unwrap_or(0);
    let dy = action.delta_y.unwrap_or(0);
    if dx == 0 && dy == 0 {
        return Err("scroll requires a non-zero delta_x or delta_y".to_string());
    }
    if !(-MAX_SCROLL_DELTA..=MAX_SCROLL_DELTA).contains(&dx)
        || !(-MAX_SCROLL_DELTA..=MAX_SCROLL_DELTA).contains(&dy)
    {
        return Err(format!(
            "scroll deltas must be between -{MAX_SCROLL_DELTA} and {MAX_SCROLL_DELTA}"
        ));
    }
    Ok((dx, dy))
}

#[cfg(target_os = "macos")]
fn scroll_steps(delta_x: i32, delta_y: i32) -> Vec<(i32, i32)> {
    let count =
        ((delta_x.abs().max(delta_y.abs()) + SCROLL_STEP_PIXELS - 1) / SCROLL_STEP_PIXELS).max(1);
    // Distribute rounding remainders so diagonal scrolling preserves both totals.
    (0..count)
        .map(|i| {
            (
                delta_x * (i + 1) / count - delta_x * i / count,
                delta_y * (i + 1) / count - delta_y * i / count,
            )
        })
        .collect()
}

#[cfg(target_os = "macos")]
fn mac_scroll_event(
    target: Option<WindowTarget>,
    point: (i32, i32),
    delta_x: i32,
    delta_y: i32,
) -> Result<CGEvent, String> {
    let event = CGEvent::new_scroll_event(
        mac_event_source()?,
        ScrollEventUnit::PIXEL,
        2,
        -delta_y,
        -delta_x,
        0,
    )
    .map_err(|_| "Unable to create a macOS scroll event".to_string())?;
    // Public deltas follow viewport movement: positive down/right. Quartz's
    // wheel signs are the opposite. Both macOS modes use this same conversion.
    event.set_location(CGPoint::new(point.0 as f64, point.1 as f64));
    if let Some(target) = target {
        mac_target_pointer_event(&event, target)?;
    }
    Ok(event)
}

#[cfg(target_os = "macos")]
fn mac_scroll(
    target: Option<WindowTarget>,
    point: (i32, i32),
    delta_x: i32,
    delta_y: i32,
) -> Result<(), String> {
    if let Some(target) = target {
        mac_post_mouse(
            target,
            CGEventType::MouseMoved,
            CGMouseButton::Left,
            point.0,
            point.1,
            0,
        )?;
    }
    // Let the application update its hover/hit-test state before the wheel input.
    thread::sleep(Duration::from_millis(30));
    for (dx, dy) in scroll_steps(delta_x, delta_y) {
        let event = mac_scroll_event(target, point, dx, dy)?;
        if let Some(target) = target {
            event.post_to_pid(target.pid);
        } else {
            event.post(CGEventTapLocation::HID);
        }
        thread::sleep(Duration::from_millis(16));
    }
    Ok(())
}

fn add_scroll_receipt(result: &mut Value, action: &ComputerAction) {
    if action.action != "scroll" {
        return;
    }
    result["effect_verified"] = json!(false);
    result["scroll"] = json!({
        "unit": if cfg!(target_os = "macos") { "pixels" } else { "wheel_steps" },
        "delta_x": action.delta_x.unwrap_or(0),
        "delta_y": action.delta_y.unwrap_or(0),
    });
    result["verification_hint"] = json!(
        "Input was sent; target scrolling is not confirmed. Compare the target scroll area before and after. \
         An unchanged image does not prove a history boundary. Background support varies by app; \
         do not automatically switch to foreground control."
    );
}

#[cfg(target_os = "macos")]
struct MacKeyboardPreparation {
    target: WindowTarget,
    receipt: Value,
}

#[cfg(target_os = "macos")]
fn mac_verify_keyboard_focus(target: WindowTarget, require_foreground: bool) -> Result<(), String> {
    let application = mac_ax_relations::application(target.pid)?;
    let focus = mac_ax_relations::focus_snapshot(&application, target.pid);
    // The resolved window is pinned for the entire sequence. A newly opened
    // sheet or a sibling document must not receive the remaining characters.
    if focus.focused_window_id != Some(target.window_id) {
        return Err("Keyboard responder changed; observe before sending further input".into());
    }
    if require_foreground
        && mac_front_process_serial_number()? != mac_process_serial_number(target.pid)?
    {
        return Err("Keyboard target is no longer the foreground application".into());
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn keyboard_failure(mut receipt: Value, code: &str, reason: &str, sent: usize) -> Value {
    receipt["ok"] = json!(false);
    receipt["error_code"] = json!(code);
    receipt["error"] = json!(reason);
    receipt["summary"] = json!(if sent == 0 {
        "Keyboard input was not sent; observe the focused window"
    } else {
        "Keyboard input was partially sent; observe before continuing"
    });
    receipt["action_dispatched"] = json!(sent > 0);
    receipt["dispatch_succeeded"] = json!(false);
    receipt["keyboard_events_sent"] = json!(sent);
    receipt["effect_verified"] = json!(false);
    receipt["retry_safe"] = json!(sent == 0);
    receipt["requires_observation"] = json!(true);
    receipt
}

#[cfg(target_os = "macos")]
fn mac_prepare_keyboard(
    target: WindowTarget,
    policy: DeliveryPolicy,
) -> Result<MacKeyboardPreparation, Value> {
    let _timing = diagnostics::Stage::new("keyboard_preparation");
    let mut receipt = json!({
        "requested_window_id": target.window_id.to_string(),
        "resolved_window_id": null, "focused_window_id": null,
        "focused_element_role": null, "focus_resolution": "unresolved",
        "focus_changed_by_tool": false,
    });
    let live = mac_all_window_info().map_err(|reason| {
        keyboard_failure(receipt.clone(), "window_snapshot_unavailable", &reason, 0)
    })?;
    if !live.iter().any(|window| {
        window.target.window_id == target.window_id && window.target.pid == target.pid
    }) {
        return Err(keyboard_failure(
            receipt,
            "target_stale",
            "The requested window disappeared before keyboard preparation",
            0,
        ));
    }
    let application = mac_ax_relations::application(target.pid).map_err(|reason| {
        keyboard_failure(receipt.clone(), "keyboard_focus_unresolved", &reason, 0)
    })?;
    let snapshot = mac_ax_relations::snapshot(&application, target.pid);
    let resolution = snapshot
        .relations
        .resolve_focus(target.window_id, snapshot.focused_window_id);
    receipt["focused_window_id"] = json!(snapshot.focused_window_id.map(|id| id.to_string()));
    receipt["focused_element_role"] = json!(snapshot.focused_element_role);
    receipt["focus_resolution"] = json!(resolution);
    if !matches!(resolution, "requested_window" | "owned_auxiliary") {
        return Err(keyboard_failure(receipt, if resolution == "other_document" {
            "keyboard_target_mismatch"
        } else { "keyboard_focus_unresolved" },
            "The focused responder cannot be proven to belong to the requested window. Observe and explicitly select the intended editor before retrying", 0));
    }
    let resolved = WindowTarget {
        window_id: snapshot.focused_window_id.unwrap(),
        ..target
    };
    receipt["resolved_window_id"] = json!(resolved.window_id.to_string());
    receipt["resolved_window"] = snapshot.relations.receipt(resolved.window_id);
    mac_verify_keyboard_focus(resolved, false).map_err(|reason| {
        keyboard_failure(receipt.clone(), "keyboard_focus_unresolved", &reason, 0)
    })?;
    let needs_activation = policy == DeliveryPolicy::AllowForeground
        && (|| {
            Ok::<_, String>(
                mac_front_process_serial_number()? != mac_process_serial_number(target.pid)?,
            )
        })()
        .map_err(|reason| {
            keyboard_failure(receipt.clone(), "keyboard_focus_unresolved", &reason, 0)
        })?;
    if needs_activation {
        // Preserve the selected responder. Never set AXMain or AXRaise here:
        // either can dismiss an active field editor belonging to the document.
        let frontmost = CFString::new("AXFrontmost");
        receipt["focus_changed_by_tool"] = Value::Null;
        let status = unsafe {
            AXUIElementSetAttributeValue(
                application.as_CFTypeRef(),
                frontmost.as_concrete_TypeRef(),
                CFBoolean::true_value().as_CFTypeRef(),
            )
        };
        if status != AX_ERROR_SUCCESS {
            return Err(keyboard_failure(
                receipt,
                "keyboard_focus_unresolved",
                &mac_ax_error("Activating keyboard target", status),
                0,
            ));
        }
        receipt["focus_changed_by_tool"] = json!(true);
        for _ in 0..25 {
            if mac_verify_keyboard_focus(resolved, true).is_ok() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
    mac_verify_keyboard_focus(resolved, policy == DeliveryPolicy::AllowForeground).map_err(
        |reason| keyboard_failure(receipt.clone(), "keyboard_focus_unresolved", &reason, 0),
    )?;
    Ok(MacKeyboardPreparation {
        target: resolved,
        receipt,
    })
}

#[cfg(target_os = "macos")]
fn mac_keycode(name: &str) -> Option<u16> {
    Some(match name.trim().to_ascii_uppercase().as_str() {
        "A" => KeyCode::ANSI_A,
        "B" => KeyCode::ANSI_B,
        "C" => KeyCode::ANSI_C,
        "D" => KeyCode::ANSI_D,
        "E" => KeyCode::ANSI_E,
        "F" => KeyCode::ANSI_F,
        "G" => KeyCode::ANSI_G,
        "H" => KeyCode::ANSI_H,
        "I" => KeyCode::ANSI_I,
        "J" => KeyCode::ANSI_J,
        "K" => KeyCode::ANSI_K,
        "L" => KeyCode::ANSI_L,
        "M" => KeyCode::ANSI_M,
        "N" => KeyCode::ANSI_N,
        "O" => KeyCode::ANSI_O,
        "P" => KeyCode::ANSI_P,
        "Q" => KeyCode::ANSI_Q,
        "R" => KeyCode::ANSI_R,
        "S" => KeyCode::ANSI_S,
        "T" => KeyCode::ANSI_T,
        "U" => KeyCode::ANSI_U,
        "V" => KeyCode::ANSI_V,
        "W" => KeyCode::ANSI_W,
        "X" => KeyCode::ANSI_X,
        "Y" => KeyCode::ANSI_Y,
        "Z" => KeyCode::ANSI_Z,
        "0" => KeyCode::ANSI_0,
        "1" => KeyCode::ANSI_1,
        "2" => KeyCode::ANSI_2,
        "3" => KeyCode::ANSI_3,
        "4" => KeyCode::ANSI_4,
        "5" => KeyCode::ANSI_5,
        "6" => KeyCode::ANSI_6,
        "7" => KeyCode::ANSI_7,
        "8" => KeyCode::ANSI_8,
        "9" => KeyCode::ANSI_9,
        "ENTER" | "RETURN" => KeyCode::RETURN,
        "TAB" => KeyCode::TAB,
        "SPACE" => KeyCode::SPACE,
        "BACKSPACE" => KeyCode::DELETE,
        "DELETE" | "DEL" => KeyCode::FORWARD_DELETE,
        "ESC" | "ESCAPE" => KeyCode::ESCAPE,
        "LEFT" | "ARROWLEFT" => KeyCode::LEFT_ARROW,
        "RIGHT" | "ARROWRIGHT" => KeyCode::RIGHT_ARROW,
        "UP" | "ARROWUP" => KeyCode::UP_ARROW,
        "DOWN" | "ARROWDOWN" => KeyCode::DOWN_ARROW,
        "HOME" => KeyCode::HOME,
        "END" => KeyCode::END,
        "PAGEUP" | "PAGE_UP" => KeyCode::PAGE_UP,
        "PAGEDOWN" | "PAGE_DOWN" => KeyCode::PAGE_DOWN,
        "F1" => KeyCode::F1,
        "F10" => KeyCode::F10,
        "F11" => KeyCode::F11,
        "F12" => KeyCode::F12,
        _ => return None,
    })
}

#[cfg(target_os = "macos")]
fn mac_key_combination(names: &[String]) -> Result<(u16, CGEventFlags), String> {
    if names.is_empty() {
        return Err("keys cannot be empty".to_string());
    }
    let mut flags = CGEventFlags::CGEventFlagNull;
    let mut key_name: Option<&str> = None;
    for name in names {
        match name.trim().to_ascii_uppercase().as_str() {
            "CMD" | "COMMAND" | "META" => flags |= CGEventFlags::CGEventFlagCommand,
            "CTRL" | "CONTROL" => flags |= CGEventFlags::CGEventFlagControl,
            "ALT" | "OPTION" => flags |= CGEventFlags::CGEventFlagAlternate,
            "SHIFT" => flags |= CGEventFlags::CGEventFlagShift,
            _ if key_name.is_none() => key_name = Some(name),
            _ => return Err("background_app keypress supports one non-modifier key".to_string()),
        }
    }
    let name = key_name.ok_or_else(|| "keypress requires a non-modifier key".to_string())?;
    let keycode = mac_keycode(name).ok_or_else(|| format!("Unknown macOS key: {name}"))?;
    Ok((keycode, flags))
}

#[cfg(target_os = "macos")]
fn mac_post_keyboard_pair(
    down: CGEvent,
    up: CGEvent,
    pid: i32,
    route: Option<(WindowTarget, bool)>,
    sent: &mut usize,
) -> Result<(), String> {
    let _timing = diagnostics::Stage::new("keyboard_dispatch");
    if let Some((target, foreground)) = route {
        mac_verify_keyboard_focus(target, foreground)?;
    }
    let foreground = route.is_some_and(|(_, foreground)| foreground);
    if foreground {
        down.post(CGEventTapLocation::HID);
    } else {
        down.post_to_pid(pid);
    }
    *sent += 1;
    // Balance every submitted key-down, including shortcuts that close a window.
    if foreground {
        up.post(CGEventTapLocation::HID);
    } else {
        up.post_to_pid(pid);
    }
    *sent += 1;
    thread::sleep(Duration::from_millis(10));
    Ok(())
}

#[cfg(all(test, target_os = "macos"))]
fn mac_press_keys(pid: i32, names: &[String]) -> Result<(), String> {
    mac_send_keys(pid, names, None)
}

#[cfg(all(test, target_os = "macos"))]
fn mac_send_keys(
    pid: i32,
    names: &[String],
    foreground: Option<WindowTarget>,
) -> Result<(), String> {
    mac_send_keys_tracked(pid, names, foreground.map(|target| (target, true)), &mut 0)
}

#[cfg(target_os = "macos")]
fn mac_send_keys_tracked(
    pid: i32,
    names: &[String],
    route: Option<(WindowTarget, bool)>,
    sent: &mut usize,
) -> Result<(), String> {
    let (keycode, flags) = mac_key_combination(names)?;
    let source = mac_event_source()?;
    let make = |down| {
        let event = CGEvent::new_keyboard_event(source.clone(), keycode, down)
            .map_err(|_| "Unable to create a macOS keyboard event".to_string())?;
        event.set_flags(flags);
        Ok::<_, String>(event)
    };
    mac_post_keyboard_pair(make(true)?, make(false)?, pid, route, sent)
}

#[cfg(all(test, target_os = "macos"))]
fn mac_type_text(pid: i32, text: &str) -> Result<(), String> {
    mac_send_text(pid, text, None)
}

#[cfg(all(test, target_os = "macos"))]
fn mac_send_text(pid: i32, text: &str, foreground: Option<WindowTarget>) -> Result<(), String> {
    mac_send_text_tracked(pid, text, foreground.map(|target| (target, true)), &mut 0)
}

#[cfg(target_os = "macos")]
fn mac_send_text_tracked(
    pid: i32,
    text: &str,
    route: Option<(WindowTarget, bool)>,
    sent: &mut usize,
) -> Result<(), String> {
    let source = mac_event_source()?;
    for character in text.chars() {
        let value = character.to_string();
        let make = |down| {
            let event = CGEvent::new_keyboard_event(source.clone(), 0, down)
                .map_err(|_| "Unable to create a macOS text event".to_string())?;
            event.set_flags(CGEventFlags::CGEventFlagNull);
            event.set_string(&value);
            Ok::<_, String>(event)
        };
        mac_post_keyboard_pair(make(true)?, make(false)?, pid, route, sent)?;
    }
    Ok(())
}

struct ScreenshotCapture {
    image: RgbaImage,
    origin_x: i32,
    origin_y: i32,
    target: String,
    component_window_ids: Vec<u32>,
    hidden_component_window_ids: Vec<u32>,
    component_capture_errors: Vec<String>,
    window_components: Vec<Value>,
    excluded_windows: Vec<Value>,
}

#[cfg(target_os = "macos")]
fn mac_capture_window(target: WindowTarget) -> Result<RgbaImage, String> {
    let readback = diagnostics::Stage::new("capture_readback");
    let bounds = CGRect::new(
        &CGPoint::new(f64::from(target.x), f64::from(target.y)),
        &CGSize::new(f64::from(target.width), f64::from(target.height)),
    );
    let Some(image) = create_image(
        bounds,
        kCGWindowListOptionIncludingWindow,
        target.window_id,
        kCGWindowImageBoundsIgnoreFraming,
    )
    .or_else(|| {
        create_image(
            unsafe { CGRectNull },
            kCGWindowListOptionIncludingWindow,
            target.window_id,
            kCGWindowImageBoundsIgnoreFraming,
        )
    }) else {
        drop(readback);
        return mac_capture_hidden_window_with_screencapture(target);
    };
    drop(readback);
    mac_decode_window_image(image, target)
}

#[cfg(target_os = "macos")]
fn mac_decode_window_image(image: CGImage, target: WindowTarget) -> Result<RgbaImage, String> {
    let _timing = diagnostics::Stage::new("capture_decode");
    if image.width() == 0 || image.height() == 0 || target.width == 0 || target.height == 0 {
        return Err(format!(
            "Window {} returned an empty capture",
            target.window_id
        ));
    }
    // Let CoreGraphics handle the source color space and byte order. Captured
    // windows can use the display profile; the PNG and image compositor need
    // a consistent sRGB, RGBA buffer. Draw directly at window-coordinate size:
    // decoding a Retina buffer and then resizing it in Rust adds a full-image
    // allocation and a costly CPU resample, especially in development builds.
    let width = target.width as usize;
    let height = target.height as usize;
    let color_space = CGColorSpace::create_with_name(unsafe { kCGColorSpaceSRGB })
        .ok_or_else(|| "Unable to create the screenshot color space".to_string())?;
    let mut context = CGContext::create_bitmap_context(
        None,
        width,
        height,
        8,
        width * 4,
        &color_space,
        CGImageAlphaInfo::CGImageAlphaPremultipliedLast as u32
            | CGImageByteOrderInfo::CGImageByteOrder32Big as u32,
    );
    context.set_interpolation_quality(CGInterpolationQuality::CGInterpolationQualityHigh);
    context.draw_image(
        CGRect::new(
            &CGPoint::new(0.0, 0.0),
            &CGSize::new(width as f64, height as f64),
        ),
        &image,
    );
    let mut rgba = context.data().to_vec();
    for pixel in rgba.chunks_exact_mut(4) {
        mac_unpremultiply_pixel(pixel);
    }
    RgbaImage::from_raw(target.width, target.height, rgba)
        .ok_or_else(|| format!("Unable to decode window {} capture", target.window_id))
}

#[cfg(target_os = "macos")]
fn mac_unpremultiply_pixel(pixel: &mut [u8]) {
    // CoreGraphics stores premultiplied color, whereas image::overlay and PNG
    // expect straight alpha. Applying alpha a second time darkens translucent
    // dialog surfaces into a mask.
    let alpha = u32::from(pixel[3]);
    if alpha == 255 {
        return;
    }
    for channel in &mut pixel[..3] {
        *channel = if alpha == 0 {
            0
        } else {
            ((u32::from(*channel) * 255 + alpha / 2) / alpha).min(255) as u8
        };
    }
}

#[cfg(target_os = "macos")]
fn mac_capture_hidden_window_with_screencapture(target: WindowTarget) -> Result<RgbaImage, String> {
    let _timing = diagnostics::Stage::new("capture_fallback");
    let output = tempfile::Builder::new()
        .prefix("crabcode-window-")
        .suffix(".png")
        .tempfile()
        .map_err(|error| format!("Unable to prepare hidden-window capture: {error}"))?;
    let path = output.path();
    let mut child = Command::new("/usr/sbin/screencapture")
        .args(["-x", "-o", "-l", &target.window_id.to_string()])
        .arg(path)
        .spawn()
        .map_err(|error| {
            format!(
                "Unable to invoke hidden-window capture for {}: {error}",
                target.window_id
            )
        })?;
    let started = std::time::Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() < Duration::from_secs(5) => {
                thread::sleep(Duration::from_millis(10));
            }
            outcome => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(match outcome {
                    Err(error) => format!("Unable to wait for window capture: {error}"),
                    _ => format!(
                        "Window {} capture timed out after 5 seconds",
                        target.window_id
                    ),
                });
            }
        }
    };
    if !status.success() {
        return Err(format!(
            "Unable to capture hidden window {}; screencapture exited with {status}",
            target.window_id
        ));
    }
    let image = xcap::image::open(path)
        .map_err(|error| {
            format!(
                "Unable to decode hidden window {}: {error}",
                target.window_id
            )
        })?
        .to_rgba8();
    Ok(normalize_image(image, target.width, target.height))
}

#[cfg(target_os = "macos")]
fn mac_capture_window_group(root: WindowTarget) -> Result<ScreenshotCapture, String> {
    let _timing = diagnostics::Stage::new("capture");
    let mut group = mac_window_group(root)?;
    for _ in 0..2 {
        let capture = mac_capture_window_group_snapshot(root, &group)?;
        let current = mac_window_group(root)?;
        if current == group {
            return Ok(capture);
        }
        // A popup can close while its backing store is being captured. Do not
        // return the old overlay after the server has ordered that window out.
        group = current;
    }
    Err("Window layout changed during capture; observe again for a current frame".to_string())
}

#[cfg(target_os = "macos")]
fn mac_capture_window_group_snapshot(
    root: WindowTarget,
    group: &MacWindowGroup,
) -> Result<ScreenshotCapture, String> {
    let root = group
        .components
        .iter()
        .find(|window| window.target.window_id == root.window_id)
        .map(|window| window.target)
        .unwrap_or(root);
    let mut image = mac_capture_window(root)?;
    let mut component_window_ids = vec![root.window_id];
    let mut hidden_component_window_ids = Vec::new();
    let mut component_capture_errors = Vec::new();

    // The Core Graphics list is front-to-back. Composite from the root toward
    // the front so each higher transient surface lands above its owner.
    for component in group.components.iter().rev() {
        if component.target.window_id == root.window_id {
            continue;
        }
        if !component.on_screen {
            hidden_component_window_ids.push(component.target.window_id);
        }
        match mac_capture_window(component.target) {
            Ok(overlay) => {
                let offset_x = i64::from(component.target.x) - i64::from(root.x);
                let offset_y = i64::from(component.target.y) - i64::from(root.y);
                xcap::image::imageops::overlay(&mut image, &overlay, offset_x, offset_y);
                component_window_ids.push(component.target.window_id);
            }
            Err(error) => component_capture_errors.push(error),
        }
    }

    Ok(ScreenshotCapture {
        image,
        origin_x: root.x,
        origin_y: root.y,
        target: format!("window:{}", root.window_id),
        component_window_ids,
        hidden_component_window_ids,
        component_capture_errors,
        window_components: group
            .components
            .iter()
            .map(|w| group.relations.receipt(w.target.window_id))
            .collect(),
        excluded_windows: group
            .excluded
            .iter()
            .map(|w| {
                let mut receipt = group.relations.receipt(w.target.window_id);
                receipt["excluded_reason"] = json!(if group.relations.kind(w.target.window_id)
                    == WindowKind::Document
                {
                    "sibling_document"
                } else {
                    "ownership_unproven"
                });
                receipt
            })
            .collect(),
    })
}

fn capture_screenshot(action: &ComputerAction) -> Result<ScreenshotCapture, String> {
    let (image, x, y, target) = if let Some(window_id) = action.window_id.as_deref() {
        #[cfg(target_os = "macos")]
        {
            return mac_capture_window_group(window_target(window_id)?);
        }

        #[cfg(not(target_os = "macos"))]
        {
            let windows = Window::all().map_err(|error| error.to_string())?;
            let window = windows
                .into_iter()
                .find(|window| {
                    window
                        .id()
                        .map(|id| id.to_string() == window_id)
                        .unwrap_or(false)
                })
                .ok_or_else(|| format!("Window not found: {window_id}"))?;
            let x = window.x().map_err(|error| error.to_string())?;
            let y = window.y().map_err(|error| error.to_string())?;
            let width = window.width().map_err(|error| error.to_string())?;
            let height = window.height().map_err(|error| error.to_string())?;
            let image = normalize_image(
                window.capture_image().map_err(|error| error.to_string())?,
                width,
                height,
            );
            (image, x, y, format!("window:{window_id}"))
        }
    } else {
        let found = monitors()?;
        let (monitor, info) = if let Some(display_id) = action.display_id.as_deref() {
            found
                .iter()
                .find(|(_, info)| info.id == display_id)
                .cloned()
                .ok_or_else(|| format!("Display not found: {display_id}"))?
        } else if let Some((x, y)) = action
            .to_x
            .zip(action.to_y)
            .or_else(|| action.x.zip(action.y))
        {
            let monitor = Monitor::from_point(x, y).map_err(|error| error.to_string())?;
            let info = display_info(&monitor)?;
            (monitor, info)
        } else {
            found
                .iter()
                .find(|(_, info)| info.primary)
                .or_else(|| found.first())
                .cloned()
                .ok_or_else(|| "No display is available".to_string())?
        };
        let image = normalize_image(
            monitor.capture_image().map_err(|error| error.to_string())?,
            info.width,
            info.height,
        );
        (image, info.x, info.y, format!("display:{}", info.id))
    };
    Ok(ScreenshotCapture {
        image,
        origin_x: x,
        origin_y: y,
        target,
        component_window_ids: Vec::new(),
        hidden_component_window_ids: Vec::new(),
        component_capture_errors: Vec::new(),
        window_components: Vec::new(),
        excluded_windows: Vec::new(),
    })
}

fn screenshot(action: &ComputerAction) -> Result<Value, String> {
    encode_screenshot_capture(capture_screenshot(action)?)
}

fn normalize_image(image: RgbaImage, coordinate_width: u32, coordinate_height: u32) -> RgbaImage {
    if image.width() == coordinate_width && image.height() == coordinate_height {
        return image;
    }
    DynamicImage::ImageRgba8(image)
        .resize_exact(coordinate_width, coordinate_height, FilterType::Triangle)
        .to_rgba8()
}

fn encode_screenshot(image: RgbaImage, x: i32, y: i32, target: String) -> Result<Value, String> {
    let width = image.width();
    let height = image.height();
    let mut bytes = Cursor::new(Vec::new());
    DynamicImage::ImageRgba8(image)
        .write_to(&mut bytes, ImageFormat::Png)
        .map_err(|error| error.to_string())?;
    if bytes.get_ref().len() > MAX_SCREENSHOT_BYTES {
        return Err("Screenshot exceeds the 20MB transport limit".to_string());
    }
    let frame_id = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos().to_string())
        .unwrap_or_else(|_| "0".to_string());
    Ok(json!({
        "data": STANDARD.encode(bytes.into_inner()),
        "media_type": "image/png",
        "width": width,
        "height": height,
        "origin_x": x,
        "origin_y": y,
        "frame_id": frame_id,
        "target": target,
    }))
}

fn encode_screenshot_capture(capture: ScreenshotCapture) -> Result<Value, String> {
    let _timing = diagnostics::Stage::new("encode");
    let mut frame = encode_screenshot(
        capture.image,
        capture.origin_x,
        capture.origin_y,
        capture.target,
    )?;
    if !capture.window_components.is_empty() {
        frame["window_components"] = json!(capture.window_components);
    }
    if !capture.excluded_windows.is_empty() {
        frame["requires_observation"] = json!(capture
            .excluded_windows
            .iter()
            .any(|w| w["excluded_reason"] == "ownership_unproven"));
        frame["excluded_windows"] = json!(capture.excluded_windows);
    }
    if !capture.component_window_ids.is_empty() {
        frame["component_window_ids"] = json!(capture.component_window_ids);
    }
    if !capture.hidden_component_window_ids.is_empty() {
        frame["hidden_component_window_ids"] = json!(capture.hidden_component_window_ids);
    }
    if !capture.component_capture_errors.is_empty() {
        frame["component_capture_errors"] = json!(capture.component_capture_errors);
    }
    if !frame["hidden_component_window_ids"].is_null()
        || !frame["component_capture_errors"].is_null()
    {
        frame["background_observation_limited"] = Value::Bool(true);
        frame["observation_warning"] = Value::String(
            "The application has an auxiliary window whose background pixels may be incomplete or unavailable"
                .to_string(),
        );
    }
    Ok(frame)
}

fn perform_action(action: &ComputerAction, enigo: &mut Enigo) -> Result<String, String> {
    match action.action.as_str() {
        "observe" | "list_displays" | "list_windows" => Ok(action.action.clone()),
        "move" => {
            enigo
                .move_mouse(
                    required(action.x, "x")?,
                    required(action.y, "y")?,
                    Coordinate::Abs,
                )
                .map_err(|error| error.to_string())?;
            Ok("Moved pointer".to_string())
        }
        "click" | "double_click" => {
            if let (Some(x), Some(y)) = (action.x, action.y) {
                enigo
                    .move_mouse(x, y, Coordinate::Abs)
                    .map_err(|error| error.to_string())?;
            }
            let button = mouse_button(action.button.as_deref())?;
            enigo
                .button(button, Direction::Click)
                .map_err(|error| error.to_string())?;
            if action.action == "double_click" {
                thread::sleep(Duration::from_millis(80));
                enigo
                    .button(button, Direction::Click)
                    .map_err(|error| error.to_string())?;
            }
            Ok(if action.action == "double_click" {
                "Double-clicked"
            } else {
                "Clicked"
            }
            .to_string())
        }
        "drag" => {
            let start_x = required(action.x, "x")?;
            let start_y = required(action.y, "y")?;
            let end_x = required(action.to_x, "to_x")?;
            let end_y = required(action.to_y, "to_y")?;
            let button = mouse_button(action.button.as_deref())?;
            enigo
                .move_mouse(start_x, start_y, Coordinate::Abs)
                .map_err(|error| error.to_string())?;
            enigo
                .button(button, Direction::Press)
                .map_err(|error| error.to_string())?;
            let duration = action.duration_ms.unwrap_or(400).min(30_000);
            let steps = 20i32;
            let movement_result = (|| {
                for step in 1..=steps {
                    let x = start_x + (end_x - start_x) * step / steps;
                    let y = start_y + (end_y - start_y) * step / steps;
                    enigo
                        .move_mouse(x, y, Coordinate::Abs)
                        .map_err(|error| error.to_string())?;
                    thread::sleep(Duration::from_millis(duration / steps as u64));
                }
                Ok::<(), String>(())
            })();
            let release_result = enigo
                .button(button, Direction::Release)
                .map_err(|error| error.to_string());
            movement_result?;
            release_result?;
            Ok("Dragged pointer".to_string())
        }
        "scroll" => {
            let (delta_x, delta_y) = scroll_delta(action)?;
            if let (Some(x), Some(y)) = (action.x, action.y) {
                enigo
                    .move_mouse(x, y, Coordinate::Abs)
                    .map_err(|error| error.to_string())?;
            }
            #[cfg(target_os = "macos")]
            {
                // A posted mouse move may not have updated the hardware
                // cursor yet. Keep the requested point instead of re-reading it.
                let point = match (action.x, action.y) {
                    (Some(x), Some(y)) => (x, y),
                    _ => enigo.location().map_err(|error| error.to_string())?,
                };
                mac_scroll(None, point, delta_x, delta_y)?;
            }
            #[cfg(not(target_os = "macos"))]
            {
                if delta_x != 0 {
                    enigo
                        .scroll(delta_x, Axis::Horizontal)
                        .map_err(|error| error.to_string())?;
                }
                if delta_y != 0 {
                    enigo
                        .scroll(delta_y, Axis::Vertical)
                        .map_err(|error| error.to_string())?;
                }
            }
            Ok("Scroll input sent; movement unverified".to_string())
        }
        "type" => {
            enigo
                .text(
                    action
                        .text
                        .as_deref()
                        .ok_or_else(|| "text is required".to_string())?,
                )
                .map_err(|error| error.to_string())?;
            Ok("Typed text".to_string())
        }
        "keypress" => {
            press_keys(enigo, action.keys.as_deref().unwrap_or_default())?;
            Ok("Pressed keys".to_string())
        }
        "open_app" => {
            let name = action
                .text
                .as_deref()
                .ok_or_else(|| "text is required".to_string())?;
            activate_app(name)?;
            Ok(format!("Opened {name}"))
        }
        #[cfg(target_os = "windows")]
        "focus_window" => {
            let id = select_focus_window(
                &window_list()?,
                action.window_id.as_deref(),
                action.text.as_deref(),
            )?;
            windows_focus::focus_window(id)?;
            Ok(format!("Focused window {id}"))
        }
        #[cfg(not(target_os = "windows"))]
        "focus_window" => {
            let name = if let Some(name) = action.text.as_deref() {
                name.to_string()
            } else {
                let id = action
                    .window_id
                    .as_deref()
                    .ok_or_else(|| "window_id or text is required".to_string())?;
                Window::all()
                    .map_err(|error| error.to_string())?
                    .iter()
                    .find(|window| {
                        window
                            .id()
                            .map(|value| value.to_string() == id)
                            .unwrap_or(false)
                    })
                    .ok_or_else(|| format!("Window not found: {id}"))?
                    .app_name()
                    .map_err(|error| error.to_string())?
            };
            focus_app(&name)?;
            Ok(format!("Focused {name}"))
        }
        "wait" => {
            thread::sleep(Duration::from_millis(
                action.duration_ms.unwrap_or(500).min(30_000),
            ));
            Ok("Waited".to_string())
        }
        other => Err(format!("Unknown Computer Use action: {other}")),
    }
}

fn execute_foreground(request: ExecuteRequest) -> Result<Value, String> {
    let mut enigo = Enigo::new(&Settings::default()).map_err(|error| error.to_string())?;
    let summary = perform_action(&request.action, &mut enigo)?;
    let cursor = enigo
        .location()
        .map(|(x, y)| json!({ "x": x, "y": y }))
        .unwrap_or(Value::Null);

    let mut result = json!({
        "ok": true,
        "mode": "foreground_desktop",
        "action": request.action.action,
        "summary": summary,
        "cursor": cursor,
    });
    add_scroll_receipt(&mut result, &request.action);
    if request.action.action == "list_displays" {
        let displays = monitors()?
            .into_iter()
            .map(|(_, info)| info)
            .collect::<Vec<_>>();
        result["displays"] = serde_json::to_value(displays).map_err(|error| error.to_string())?;
    } else if request.action.action == "list_windows" {
        result["windows"] = Value::Array(window_list()?);
    }

    let capture = request.action.action == "observe"
        || request.action.include_screenshot.unwrap_or(!matches!(
            request.action.action.as_str(),
            "list_displays" | "list_windows" | "wait"
        ));
    if capture {
        if request.action.action == "scroll" {
            thread::sleep(Duration::from_millis(SCROLL_SETTLE_MS));
        }
        match screenshot(&request.action) {
            Ok(frame) => result["screenshot"] = frame,
            Err(error) if request.action.action == "observe" => return Err(error),
            Err(error) => result["screenshot_error"] = Value::String(error),
        }
    }
    Ok(result)
}

#[cfg(target_os = "macos")]
#[derive(Debug, Clone, Copy)]
struct ScreenshotVisualChange {
    detected: bool,
    changed_pixels: u32,
    sampled_pixels: u32,
}

#[cfg(target_os = "macos")]
fn screenshot_visual_change(
    before: &ScreenshotCapture,
    after: &ScreenshotCapture,
    point: (i32, i32),
) -> Option<ScreenshotVisualChange> {
    if before.origin_x != after.origin_x
        || before.origin_y != after.origin_y
        || before.target != after.target
        || before.image.dimensions() != after.image.dimensions()
    {
        return None;
    }
    let (width, height) = before.image.dimensions();
    let local_x = point.0.checked_sub(before.origin_x)?;
    let local_y = point.1.checked_sub(before.origin_y)?;
    if local_x < 0 || local_y < 0 || local_x >= width as i32 || local_y >= height as i32 {
        return None;
    }

    // Verify only the clicked neighbourhood. Whole-window comparison produced
    // false positives in live chat applications when an unrelated badge or
    // timestamp changed while the intended control ignored the click.
    const RADIUS: i32 = 64;
    const MIN_CHANGED_PIXELS: u32 = 24;
    const MIN_CHANNEL_DELTA: u16 = 48;
    let left = (local_x - RADIUS).max(0) as u32;
    let right = (local_x + RADIUS + 1).min(width as i32) as u32;
    let top = (local_y - RADIUS).max(0) as u32;
    let bottom = (local_y + RADIUS + 1).min(height as i32) as u32;
    let mut changed_pixels = 0u32;
    for pixel_y in top..bottom {
        for pixel_x in left..right {
            let before_pixel = before.image.get_pixel(pixel_x, pixel_y);
            let after_pixel = after.image.get_pixel(pixel_x, pixel_y);
            let delta = before_pixel
                .0
                .iter()
                .zip(after_pixel.0.iter())
                .take(3)
                .map(|(before, after)| u16::from(before.abs_diff(*after)))
                .sum::<u16>();
            if delta >= MIN_CHANNEL_DELTA {
                changed_pixels += 1;
            }
        }
    }
    let sampled_pixels = (right - left) * (bottom - top);
    Some(ScreenshotVisualChange {
        detected: changed_pixels >= MIN_CHANGED_PIXELS,
        changed_pixels,
        sampled_pixels,
    })
}

#[cfg(target_os = "macos")]
fn finish_background_click(
    action: &ComputerAction,
    mut result: Value,
    screenshot: Result<ScreenshotCapture, String>,
) -> Value {
    match screenshot {
        Ok(capture) => {
            if action.include_screenshot.unwrap_or(true) {
                match encode_screenshot_capture(capture) {
                    Ok(frame) => {
                        if frame["background_observation_limited"] == Value::Bool(true) {
                            result["background_observation_limited"] = Value::Bool(true);
                            result["observation_warning"] = frame["observation_warning"].clone();
                        }
                        result["screenshot"] = frame;
                    }
                    Err(error) => result["screenshot_error"] = Value::String(error),
                }
            }
        }
        Err(error) => {
            result["screenshot_error"] = Value::String(error);
        }
    }
    result
}

#[cfg(target_os = "macos")]
#[derive(Clone, Copy)]
enum ClickFailureStage {
    TargetVisibility,
    WindowPreparation,
    Activation,
    LayoutValidation,
    BaselineCapture,
}

#[cfg(target_os = "macos")]
struct ClickPreparationFailure {
    stage: ClickFailureStage,
    reason: String,
}

#[cfg(target_os = "macos")]
impl ClickFailureStage {
    fn failure(self, reason: String) -> ClickPreparationFailure {
        ClickPreparationFailure {
            stage: self,
            reason,
        }
    }

    fn diagnostics(self) -> (&'static str, &'static str, &'static str) {
        match self {
            Self::TargetVisibility => (
                "target_visibility",
                "background_click_target_offscreen",
                "Target window is off-screen; no click was sent",
            ),
            Self::WindowPreparation => (
                "window_preparation",
                "background_click_window_preparation_failed",
                "Target window preparation failed; no mouse click was sent",
            ),
            Self::Activation => (
                "activation",
                "background_click_activation_failed",
                "Target activation could not be confirmed or maintained; no mouse click was sent",
            ),
            Self::LayoutValidation => (
                "layout_validation",
                "background_click_layout_validation_failed",
                "Window layout validation failed; no mouse click was sent",
            ),
            Self::BaselineCapture => (
                "baseline_capture",
                "background_click_baseline_capture_failed",
                "Pre-click screenshot capture failed; no mouse click was sent",
            ),
        }
    }
}

#[cfg(target_os = "macos")]
fn failed_background_click_preparation(
    action: &ComputerAction,
    target: WindowTarget,
    event_target: WindowTarget,
    point: (i32, i32),
    failure: ClickPreparationFailure,
) -> Value {
    let (stage, error_code, summary) = failure.stage.diagnostics();
    json!({
        "ok": false,
        "mode": "background_app",
        "action": action.action,
        "summary": summary,
        "error_code": error_code,
        "error": failure.reason,
        "failure_stage": stage,
        "dispatch_status": "not_sent",
        "effect_status": "not_checked",
        "cursor": { "x": point.0, "y": point.1 },
        "coordinate_space": "window",
        "window_origin": { "x": target.x, "y": target.y },
        "dispatch_window_id": event_target.window_id.to_string(),
        "dispatch_succeeded": false,
        "effect_verified": false,
        "visual_change_detected": false,
        "input_method": "none",
        "verification_method": "not_performed",
        "foreground_activated": false,
        "foreground_verification": "not_dispatched",
        "real_cursor_moved": false,
    })
}

#[cfg(target_os = "macos")]
fn record_click_foreground_change(result: &mut Value, verification: Result<bool, String>) {
    // Focus changes are allowed in application mode. This is diagnostic only;
    // neither activation nor a failed probe changes the input/effect result.
    result["foreground_verification"] = json!("window_server_poll_2ms");
    match verification {
        Ok(activated) => {
            result["foreground_activated"] = json!(activated);
        }
        Err(error) => {
            result["foreground_verification"] = json!("unavailable");
            result["foreground_warning"] = json!(error);
            result["foreground_activated"] = Value::Null;
        }
    }
}

#[cfg(target_os = "macos")]
fn record_click_outcome(result: &mut Value, dispatched: bool, visual_change: Option<bool>) {
    let changed = visual_change.unwrap_or(false);
    // Dispatch is the execution result. Pixel changes are only evidence for
    // the model to interpret, not a prerequisite for a successful tool call.
    result["ok"] = json!(dispatched);
    result["dispatch_succeeded"] = json!(dispatched);
    result["action_dispatched"] = if dispatched { json!(true) } else { Value::Null };
    result["dispatch_status"] = json!(if dispatched { "sent" } else { "uncertain" });
    result["effect_status"] = json!(if changed {
        "change_detected"
    } else {
        "no_change_detected"
    });
    result["effect_verified"] = json!(changed);
    result["visual_change_detected"] = json!(changed);
    result["summary"] = json!(if dispatched && changed {
        "Click dispatched"
    } else if dispatched {
        "Click dispatched; effect is unverified"
    } else {
        "Click may have executed but was not acknowledged; observe before retrying"
    });
    if !changed {
        result["verification_warning"] = json!(
            "Click effect is unverified. Inspect the returned screenshot or observe again to judge the result; do not repeat the click solely because no visual change was detected."
        );
    }
    if !dispatched {
        result["failure_stage"] = json!("accessibility_dispatch");
        result["error_code"] = json!("background_click_dispatch_unverified");
        result["error"] = json!(
            "The accessibility action may have arrived but dispatch was not acknowledged; observe before deciding whether another action is needed"
        );
    }
    if visual_change.is_none() {
        result["effect_status"] = json!("unavailable");
        result["verification_method"] = json!("unavailable");
        result["verification_warning"] = json!(
            "Click effect could not be checked because result screenshots are unavailable or not comparable. Observe before deciding whether another action is needed."
        );
        if dispatched {
            result["summary"] = json!("Click dispatched; effect verification is unavailable");
        }
    }
}

#[cfg(target_os = "macos")]
fn execute_foreground_allowed_window_click(
    action: &ComputerAction,
    target: WindowTarget,
    legacy_ax_hit_test: bool,
) -> Result<Value, String> {
    let point = background_local_point(action, target)?;
    let (x, y) = background_point(action, target)?;
    let event_window = match mac_background_event_target(target, (x, y)) {
        Ok(window) => window,
        Err(reason) => return Ok(unresolved_pointer_target(action, reason)),
    };
    let event_target = event_window.target;
    let is_single_left_click = action.action == "click"
        && action
            .button
            .as_deref()
            .unwrap_or("left")
            .eq_ignore_ascii_case("left");
    if !event_window.on_screen {
        let reason = "The target surface is off-screen; focus it and observe again before clicking"
            .to_string();
        return Ok(finish_background_click(
            action,
            failed_background_click_preparation(
                action,
                target,
                event_target,
                point,
                ClickFailureStage::TargetVisibility.failure(reason),
            ),
            mac_capture_window_group(target),
        ));
    }
    // Only the legacy semantic path can compare a pre-input screenshot without
    // activation contaminating the comparison. Coordinate clicks need one
    // resulting frame, not two unused baselines before dispatch.
    let before = if legacy_ax_hit_test && is_single_left_click {
        match mac_capture_window_group(target) {
            Ok(frame) => Some(frame),
            Err(reason) => {
                return Ok(failed_background_click_preparation(
                    action,
                    target,
                    event_target,
                    point,
                    ClickFailureStage::BaselineCapture.failure(reason),
                ))
            }
        }
    } else {
        None
    };
    let foreground_monitor = MacForegroundMonitor::start(target.pid);

    // AX-capable channels expose semantic press separately. Their coordinate
    // click is an explicit pixel-based decision and must not invoke AX again.
    // Preserve AX hit-testing for legacy channels; neither route replays an
    // acknowledged/uncertain action automatically. Strict input stays below
    // background_input's exact profile gate.
    let semantic_dispatch = if legacy_ax_hit_test && is_single_left_click {
        mac_ax_press(event_target, x, y)
    } else {
        MacAxPressOutcome::Unsupported {
            reason: "The requested mouse gesture requires window-targeted mouse events".to_string(),
        }
    };
    let uses_mouse = matches!(&semantic_dispatch, MacAxPressOutcome::Unsupported { .. });
    if uses_mouse {
        // All image work is before activation or after input. A prepared-window
        // screenshot used to leave seconds between activation and mouse-down.
        // A legacy baseline also includes activation changes, so only semantic
        // input can use it as click-effect evidence below.
        let preparation = mac_prepare_mouse_window(event_target).and_then(|_| {
                let _timing = diagnostics::Stage::new("mouse_ready_to_dispatch");
                mac_validate_mouse_layout(target, event_target, (x, y))
                    .map_err(|reason| ClickFailureStage::LayoutValidation.failure(reason))?;
                if mac_front_process_serial_number()
                    .map_err(|reason| ClickFailureStage::Activation.failure(reason))?
                    != mac_process_serial_number(target.pid)
                        .map_err(|reason| ClickFailureStage::Activation.failure(reason))? {
                    return Err(ClickFailureStage::Activation.failure("The target lost activation before mouse input; no click was sent. Observe again before clicking".to_string()));
                }
                Ok(())
            });
        match preparation {
            Ok(()) => {}
            Err(failure) => {
                let mut result = failed_background_click_preparation(
                    action,
                    target,
                    event_target,
                    point,
                    failure,
                );
                record_click_foreground_change(
                    &mut result,
                    foreground_monitor.and_then(MacForegroundMonitor::finish),
                );
                return Ok(finish_background_click(
                    action,
                    result,
                    mac_capture_window_group(target),
                ));
            }
        }
        mac_click(
            event_target,
            x,
            y,
            action.button.as_deref(),
            if action.action == "double_click" {
                2
            } else {
                1
            },
        )?;
    }

    thread::sleep(Duration::from_millis(CLICK_SETTLE_MS));
    let mut after = mac_capture_window_group(target);
    let mut visual_change = after
        .as_ref()
        .ok()
        .filter(|_| !uses_mouse)
        .and_then(|frame| {
            before
                .as_ref()
                .and_then(|before| screenshot_visual_change(before, frame, (x, y)))
        });
    if visual_change.is_some_and(|change| !change.detected) && after.is_ok() {
        thread::sleep(Duration::from_millis(CLICK_SETTLE_MS));
        after = mac_capture_window_group(target);
        visual_change = after.as_ref().ok().and_then(|frame| {
            before
                .as_ref()
                .and_then(|before| screenshot_visual_change(before, frame, (x, y)))
        });
    }
    let mut result = json!({
        "mode": "background_app",
        "action": action.action,
        "cursor": { "x": point.0, "y": point.1 },
        "coordinate_space": "window",
        "window_origin": { "x": target.x, "y": target.y },
        "dispatch_window_id": event_target.window_id.to_string(),
        "visual_changed_pixels": visual_change.map(|change| change.changed_pixels),
        "visual_sampled_pixels": visual_change.map(|change| change.sampled_pixels),
        "input_method": if uses_mouse {
            "quartz_event"
        } else {
            "accessibility_action"
        },
        "verification_method": "screenshot_difference",
        "real_cursor_moved": false,
    });
    record_click_outcome(
        &mut result,
        uses_mouse || matches!(&semantic_dispatch, MacAxPressOutcome::Performed { .. }),
        visual_change.map(|change| change.detected),
    );
    if uses_mouse {
        result["verification_warning"] = json!(
            "Click dispatched. No screenshot was taken between activation and input, so pixel changes cannot independently verify the click's effect. Inspect the returned state; do not repeat input solely because effect_verified is false."
        );
    }
    match semantic_dispatch {
        MacAxPressOutcome::Performed { action: ax_action } => {
            result["accessibility_action"] = json!(ax_action);
        }
        MacAxPressOutcome::Uncertain { reason } => {
            result["dispatch_warning"] = json!(reason);
        }
        MacAxPressOutcome::Unsupported { reason } => {
            result["fallback_reason"] = json!(reason);
        }
    }
    let mut result = finish_background_click(action, result, after);
    record_click_foreground_change(
        &mut result,
        foreground_monitor.and_then(|monitor| monitor.finish()),
    );
    Ok(result)
}

#[cfg(target_os = "macos")]
fn execute_background(request: ExecuteRequest) -> Result<Value, String> {
    let action = &request.action;
    let Some(id) = action.window_id.as_deref() else {
        return execute_background_inner(&request, None);
    };
    // Pin the identity and geometry from the same pre-action enumeration.
    let before = match window_list() {
        Ok(windows) => windows,
        Err(reason) => {
            return Ok(
                json!({"ok": false, "error_code": "window_snapshot_unavailable",
            "error": reason, "action": action.action, "action_dispatched": false,
            "dispatch_succeeded": false, "requires_observation": true}),
            )
        }
    };
    let Some(window) = before.iter().find(|w| w["id"].as_str() == Some(id)) else {
        let mut result = window_lifecycle::stale_before_action(id, &before);
        result["action"] = json!(action.action);
        return Ok(result);
    };
    let target = WindowTarget {
        window_id: id.parse().map_err(|_| "Invalid window ID")?,
        pid: window["pid"].as_i64().ok_or("Missing window PID")? as i32,
        x: window["x"].as_i64().ok_or("Missing window X")? as i32,
        y: window["y"].as_i64().ok_or("Missing window Y")? as i32,
        width: window["width"].as_u64().ok_or("Missing window width")? as u32,
        height: window["height"].as_u64().ok_or("Missing window height")? as u32,
    };
    let read_only = policy::observation_only(&action.action);
    let mut result = match execute_background_inner(&request, Some(target)) {
        Ok(result) => result,
        Err(error) => json!({"ok": false, "action": action.action, "error": error,
            "action_dispatched": if read_only { Some(false) } else { None },
            "effect_verified": false}),
    };
    if result.get("action_dispatched").is_none() {
        result["action_dispatched"] = if read_only {
            json!(false)
        } else {
            result
                .get("dispatch_succeeded")
                .cloned()
                .unwrap_or(Value::Null)
        };
    }
    let after = window_list();
    window_lifecycle::record(
        &mut result,
        id,
        &before,
        after.as_ref().map(Vec::as_slice).map_err(String::as_str),
    );
    if request.owner.is_some()
        && !read_only
        && !matches!(
            action.action.as_str(),
            "press" | "set_value" | "perform_action"
        )
        && result["action_dispatched"] != false
        && result["window_lifecycle"]["target_resolvable"] == true
    {
        if action.action == "scroll" {
            thread::sleep(Duration::from_millis(SCROLL_SETTLE_MS));
        }
        // Observe only: an input receipt is never replaced or replayed. The
        // lifecycle check above must confirm the target before reading it again.
        let observed = mac_accessibility::observe(&request, target);
        if observed["ok"] == true {
            result["accessibility"] = observed["accessibility"].clone();
            if action.include_screenshot != Some(true) {
                if let Some(frame) = result.as_object_mut().unwrap().remove("screenshot") {
                    result["preview_screenshot"] = frame;
                }
            }
            result["observation_kind"] = json!(if result.get("screenshot").is_some() {
                "ax_and_screenshot"
            } else {
                "ax"
            });
        } else {
            result["ax_error"] = observed["error"].clone();
            result["fallback_reason"] = observed["error_code"].clone();
        }
    }
    // Both coordinate and semantic input must return pixels when the resulting
    // AX tree has no content. Preserve the input receipt even if observation
    // fails; never retry input to obtain a better observation.
    if request.owner.is_some()
        && !read_only
        && result["action_dispatched"] != false
        && result["window_lifecycle"]["target_resolvable"] == true
        && result.get("accessibility").is_none()
        && result.get("ax_error").is_some()
    {
        if action.include_screenshot != Some(false) && result.get("screenshot").is_none() {
            match mac_capture_window_group(target).and_then(encode_screenshot_capture) {
                Ok(frame) => result["screenshot"] = frame,
                Err(error) => result["screenshot_error"] = json!(error),
            }
        }
        if result.get("screenshot").is_some() {
            result["observation_kind"] = json!("screenshot");
        }
    }
    // The monitor keeps a visual preview even when the model observes AX only.
    // Capture under the same pinned target/lock without issuing another AX
    // observation, which would invalidate the element references just returned.
    // Desktop separates this transport-only frame from the Core tool result.
    if request.owner.is_some()
        && result.get("accessibility").is_some()
        && result.get("screenshot").is_none()
        && result.get("preview_screenshot").is_none()
        && result["window_lifecycle"]["target_resolvable"] == true
    {
        match mac_capture_window_group(target).and_then(encode_screenshot_capture) {
            Ok(frame) => result["preview_screenshot"] = frame,
            Err(error) => result["preview_screenshot_error"] = json!(error),
        }
    }
    if action.include_window_observations == Some(true)
        && action.include_screenshot != Some(false)
        && (action.observation.as_deref() != Some("ax") || action.include_screenshot == Some(true))
        && result["window_lifecycle"]["target_resolvable"] == true
    {
        window_observation::attach(&mut result, target);
    }
    result["retry_safe"] =
        json!(result["action_dispatched"] == false && result["retry_safe"] != false);
    Ok(result)
}

#[cfg(target_os = "macos")]
fn execute_background_inner(
    request: &ExecuteRequest,
    pinned_target: Option<WindowTarget>,
) -> Result<Value, String> {
    let delivery_policy = request.delivery_policy;
    let action = &request.action;
    if matches!(
        action.action.as_str(),
        "press" | "set_value" | "perform_action"
    ) {
        return Ok(mac_accessibility::act(
            request,
            pinned_target.ok_or("Element actions require window_id")?,
        ));
    }
    if action.action == "observe"
        && action.observation.as_deref() != Some("screenshot")
        && request.owner.is_some()
    {
        let target = pinned_target.ok_or("AX observe requires window_id")?;
        let mut observed = mac_accessibility::observe(request, target);
        if observed["ok"] == true || action.observation.as_deref() == Some("ax") {
            if action.include_screenshot == Some(true) {
                match mac_capture_window_group(target).and_then(encode_screenshot_capture) {
                    Ok(frame) => {
                        observed["screenshot"] = frame;
                        observed["observation_kind"] =
                            json!(if observed.get("accessibility").is_some() {
                                "ax_and_screenshot"
                            } else {
                                "screenshot"
                            });
                    }
                    Err(reason) => observed["screenshot_error"] = json!(reason),
                }
            }
            return Ok(observed);
        }
        // AX is unavailable or contains no useful content. Pixel observation
        // is read-only and does not change the input delivery policy.
        let mut pixel_request = ExecuteRequest {
            mode: request.mode,
            target_scope: request.target_scope,
            delivery_policy,
            owner: request.owner.clone(),
            action: action.clone(),
        };
        pixel_request.action.observation = Some("screenshot".into());
        let mut result = execute_background_inner(&pixel_request, Some(target))?;
        result["ax_error"] = observed["error"].clone();
        result["fallback_reason"] = observed["error_code"].clone();
        return Ok(result);
    }
    if action.action == "observe" || !policy::observation_only(&action.action) {
        if let Some(owner) = &request.owner {
            mac_accessibility::invalidate(owner);
        }
    }
    if matches!(
        action.action.as_str(),
        "move"
            | "click"
            | "double_click"
            | "drag"
            | "scroll"
            | "type"
            | "keypress"
            | "focus_window"
    ) {
        mac_require_input_permission()?;
    }
    let target = || {
        pinned_target.ok_or_else(|| {
            "background_app mode requires window_id; call list_windows first".to_string()
        })
    };
    if delivery_policy == DeliveryPolicy::StrictBackground
        && !policy::observation_only(&action.action)
    {
        if action.action == "open_app" {
            return Ok(json!({"ok": false, "action": action.action,
                "error_code": "background_delivery_unsupported",
                "error": "Application launch has not passed strict-background isolation validation",
                "action_dispatched": false, "effect_verified": false,
                "focus_isolation": "preserved", "input_method": "none"}));
        }
        return Ok(background_input::execute(action, target()?));
    }
    let mut cursor = Value::Null;
    let mut keyboard_receipt = None;
    let summary = match action.action.as_str() {
        "observe" => {
            target()?;
            "Observed application window".to_string()
        }
        "list_windows" => "Listed application windows".to_string(),
        "list_displays" => return Err(
            "background_app mode does not expose the full desktop or displays; use list_windows"
                .to_string(),
        ),
        "move" => {
            let target = target()?;
            let local = background_local_point(action, target)?;
            let (x, y) = background_point(action, target)?;
            let event_target = match mac_background_event_target(target, (x, y)) {
                Ok(window) => window.target,
                Err(reason) => return Ok(unresolved_pointer_target(action, reason)),
            };
            mac_post_mouse(
                event_target,
                CGEventType::MouseMoved,
                CGMouseButton::Left,
                x,
                y,
                0,
            )?;
            cursor = json!({ "x": local.0, "y": local.1 });
            "Moved application pointer".to_string()
        }
        "click" | "double_click" => {
            return execute_foreground_allowed_window_click(
                action,
                target()?,
                request.owner.is_none(),
            );
        }
        "drag" => {
            let target = target()?;
            let start = background_point(action, target)?;
            let end_action = ComputerAction {
                include_window_observations: action.include_window_observations,
                action: action.action.clone(),
                x: action.to_x,
                y: action.to_y,
                to_x: None,
                to_y: None,
                button: None,
                delta_x: None,
                delta_y: None,
                text: None,
                keys: None,
                display_id: None,
                window_id: action.window_id.clone(),
                duration_ms: None,
                include_screenshot: None,
                observation: None,
                snapshot_id: None,
                element_id: None,
                ax_action: None,
            };
            let end_local = background_local_point(&end_action, target)?;
            let end = background_point(&end_action, target)?;
            let event_target = match mac_background_event_target(target, start) {
                Ok(window) => window.target,
                Err(reason) => return Ok(unresolved_pointer_target(action, reason)),
            };
            mac_drag(
                event_target,
                start,
                end,
                action.button.as_deref(),
                action.duration_ms.unwrap_or(400).min(30_000),
            )?;
            cursor = json!({ "x": end_local.0, "y": end_local.1 });
            "Dragged in application window".to_string()
        }
        "scroll" => {
            let (delta_x, delta_y) = scroll_delta(action)?;
            let target = target()?;
            let local = background_local_point(action, target)?;
            let point = background_point(action, target)?;
            let event_target = match mac_background_event_target(target, point) {
                Ok(window) => window.target,
                Err(reason) => return Ok(unresolved_pointer_target(action, reason)),
            };
            mac_scroll(Some(event_target), point, delta_x, delta_y)?;
            cursor = json!({ "x": local.0, "y": local.1 });
            "Scroll input sent to application window; movement unverified".to_string()
        }
        "type" | "keypress" => {
            let target = target()?;
            if action.action == "type" && action.text.is_none() {
                return Err("text is required".into());
            }
            let keys = action.keys.as_deref().unwrap_or_default();
            if action.action == "keypress" {
                mac_key_combination(keys)?;
            }
            let prepared = match mac_prepare_keyboard(target, delivery_policy) {
                Ok(prepared) => prepared,
                Err(mut failure) => {
                    failure["action"] = json!(action.action);
                    return Ok(failure);
                }
            };
            let mut sent = 0;
            let route = Some((
                prepared.target,
                delivery_policy == DeliveryPolicy::AllowForeground,
            ));
            let outcome = if action.action == "type" {
                mac_send_text_tracked(
                    target.pid,
                    action.text.as_deref().unwrap(),
                    route,
                    &mut sent,
                )
            } else {
                mac_send_keys_tracked(target.pid, keys, route, &mut sent)
            };
            let mut receipt = prepared.receipt;
            if let Err(reason) = outcome {
                let mut failure =
                    keyboard_failure(receipt, "keyboard_dispatch_interrupted", &reason, sent);
                failure["action"] = json!(action.action);
                return Ok(failure);
            }
            receipt["action_dispatched"] = json!(sent > 0);
            receipt["dispatch_succeeded"] = json!(true);
            receipt["keyboard_events_sent"] = json!(sent);
            keyboard_receipt = Some(receipt);
            "Keyboard input sent; intended effect unverified".to_string()
        }
        "open_app" => {
            let name = action
                .text
                .as_deref()
                .ok_or_else(|| "text is required".to_string())?;
            let status = Command::new("open")
                .args(["-g", "-a", name])
                .status()
                .map_err(|error| format!("Unable to open app in background: {error}"))?;
            if !status.success() {
                return Err(format!(
                    "Unable to open app in background; process exited with {status}"
                ));
            }
            format!("Opened {name} in background")
        }
        "focus_window" => {
            let target = target()?;
            // Only this explicit action may raise a different document.
            let (prepared, changed) = match mac_prepare_keyboard(target, delivery_policy) {
                Ok(prepared) => (prepared, false),
                Err(mut failure) => {
                    // An activation/recheck failure is not permission to raise
                    // the parent over a responder that was already resolved.
                    if delivery_policy == DeliveryPolicy::StrictBackground
                        || failure["focus_resolution"] == "owned_auxiliary"
                        || failure["focus_resolution"] == "requested_window"
                        || failure["error_code"] == "target_stale"
                    {
                        failure["action"] = json!(action.action);
                        return Ok(failure);
                    }
                    if let Err(error) = mac_prepare_mouse_window(target) {
                        failure["error"] = json!(error.reason);
                        failure["action"] = json!(action.action);
                        failure["focus_changed_by_tool"] = Value::Null;
                        failure["action_dispatched"] = Value::Null;
                        return Ok(failure);
                    }
                    match mac_prepare_keyboard(target, delivery_policy) {
                        Ok(prepared) => (prepared, true),
                        Err(mut failure) => {
                            failure["focus_changed_by_tool"] = json!(true);
                            failure["action_dispatched"] = json!(true);
                            failure["action"] = json!(action.action);
                            return Ok(failure);
                        }
                    }
                }
            };
            let mut receipt = prepared.receipt;
            if changed {
                receipt["focus_changed_by_tool"] = json!(true);
            }
            keyboard_receipt = Some(receipt);
            format!("Verified keyboard focus for window {}", target.window_id)
        }
        "wait" => {
            thread::sleep(Duration::from_millis(
                action.duration_ms.unwrap_or(500).min(30_000),
            ));
            "Waited".to_string()
        }
        other => return Err(format!("Unknown Computer Use action: {other}")),
    };

    let mut result = json!({
        "ok": true,
        "mode": "background_app",
        "action": action.action,
        "summary": summary,
        "cursor": cursor,
        "coordinate_space": "window",
    });
    result["action_dispatched"] = json!(!policy::observation_only(&action.action));
    if let Some(receipt) = keyboard_receipt {
        result
            .as_object_mut()
            .unwrap()
            .extend(receipt.as_object().unwrap().clone());
    }
    add_scroll_receipt(&mut result, action);
    if action.action == "list_windows" {
        result["windows"] = Value::Array(window_list()?);
    }

    if matches!(action.action.as_str(), "type" | "keypress") {
        result["delivery_route"] = json!(if delivery_policy == DeliveryPolicy::AllowForeground {
            "verified_foreground_keyboard"
        } else {
            "pid_targeted_keyboard"
        });
        result["effect_verified"] = json!(false);
        result["verification_hint"] = json!("Keyboard events were sent after checking the target responder; the requested operation is not verified. Do not retry solely because the screenshot is unchanged.");
    }

    let capture = action.action == "observe"
        || action.include_screenshot.unwrap_or(
            request.owner.is_none()
                && !matches!(action.action.as_str(), "list_windows" | "wait" | "open_app"),
        );
    if capture {
        result["observation_kind"] = json!("screenshot");
        if action.action == "scroll" {
            thread::sleep(Duration::from_millis(SCROLL_SETTLE_MS));
        }
        let frame = match pinned_target {
            Some(target) => mac_capture_window_group(target).and_then(encode_screenshot_capture),
            None => screenshot(action),
        };
        match frame {
            Ok(frame) => {
                if frame["background_observation_limited"] == Value::Bool(true) {
                    result["background_observation_limited"] = Value::Bool(true);
                    result["observation_warning"] = frame["observation_warning"].clone();
                    if action.action == "observe" {
                        result["summary"] = Value::String(
                            "Observed an application window group; a hidden auxiliary window may have incomplete pixels"
                                .to_string(),
                        );
                    }
                }
                if frame["requires_observation"] == true {
                    result["requires_observation"] = json!(true);
                }
                result["screenshot"] = frame;
            }
            Err(error) if action.action == "observe" => return Err(error),
            Err(error) => result["screenshot_error"] = Value::String(error),
        }
    }
    Ok(result)
}

#[cfg(not(target_os = "macos"))]
fn execute_background(_request: ExecuteRequest) -> Result<Value, String> {
    Err("background_app mode is unavailable on this platform".to_string())
}

pub(crate) fn execute(request: ExecuteRequest) -> Result<Value, String> {
    // Prevent another Computer Use session from changing global input state
    // during a strict provider's preparation, dispatch or cleanup.
    static INPUT: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _input = INPUT
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    diagnostics::measure(|| execute_inner(request))
}

fn execute_inner(request: ExecuteRequest) -> Result<Value, String> {
    // This gate precedes permission probes, AX writes, application launch and
    // every native dispatch path. Direct Tauri calls receive the same policy.
    let legacy_scope = request.mode.map(|mode| match mode {
        ComputerUseMode::BackgroundApp => TargetScope::AppWindow,
        ComputerUseMode::ForegroundDesktop => TargetScope::Desktop,
    });
    if let (Some(legacy), Some(scope)) = (legacy_scope, request.target_scope) {
        if legacy != scope {
            return Err("Conflicting Computer Use mode and target_scope".to_string());
        }
    }
    let scope = request
        .target_scope
        .or(legacy_scope)
        .unwrap_or(TargetScope::AppWindow);
    let policy = request.delivery_policy;
    let action_name = request.action.action.clone();
    if let Some(reason) = validate_ax_request(&request, scope) {
        return Ok(
            json!({"ok": false, "action": action_name, "error_code": "invalid_ax_request",
            "error": reason, "summary": reason, "action_dispatched": false,
            "dispatch_succeeded": false, "retry_safe": true, "effect_verified": false}),
        );
    }
    if let Some(reason) = policy::rejection(scope, policy, &action_name) {
        return Ok(json!({
            "ok": false, "action": action_name,
            "target_scope": scope, "delivery_policy": policy,
            "error_code": "background_delivery_unsupported",
            "error": reason, "summary": "This action is not supported in strict background mode; no input was sent.",
            "action_dispatched": false, "dispatch_succeeded": false,
            "effect_verified": false, "retry_safe": true,
            "focus_isolation": "unavailable", "foreground_activated": false,
            "input_method": "none", "required_delivery_policy": "allow_foreground",
        }));
    }
    let read_only = policy::observation_only(&action_name);
    #[cfg(target_os = "macos")]
    if scope == TargetScope::Desktop && (action_name == "observe" || !read_only) {
        if let Some(owner) = &request.owner {
            mac_accessibility::invalidate(owner);
        }
    }
    let outcome = match scope {
        TargetScope::AppWindow => execute_background(request),
        TargetScope::Desktop => execute_foreground(request),
    };
    let mut result = match outcome {
        Ok(result) => result,
        Err(error) => json!({
            "ok": false, "action": action_name, "error": error,
            "summary": "Computer Use execution failed",
            // An exception can occur after a down event or AX action. Never
            // turn that uncertainty into a safe-to-retry no-dispatch receipt.
            "action_dispatched": if read_only { Some(false) } else { None },
            "retry_safe": read_only, "effect_verified": false,
        }),
    };
    result["target_scope"] = json!(scope);
    result["delivery_policy"] = json!(policy);
    if result.get("focus_isolation").is_none() {
        result["focus_isolation"] = json!(if read_only {
            "preserved"
        } else if policy == DeliveryPolicy::StrictBackground {
            match result.get("foreground_activated").and_then(Value::as_bool) {
                Some(false) => "preserved",
                Some(true) => "violated",
                None => "unavailable",
            }
        } else {
            "unavailable"
        });
    }
    if result.get("action_dispatched").is_none() {
        result["action_dispatched"] = if read_only {
            json!(false)
        } else if result["error_code"] == "background_click_dispatch_unverified" {
            Value::Null
        } else {
            result
                .get("dispatch_succeeded")
                .cloned()
                .unwrap_or(json!(true))
        };
    }
    // Retrying a delivered input is not safe even when pixels did not change.
    result["retry_safe"] =
        json!(result["action_dispatched"] == false && result["retry_safe"] != false);
    if result.get("effect_verified").is_none() {
        result["effect_verified"] = json!(false);
    }
    Ok(result)
}

fn validate_ax_request(request: &ExecuteRequest, scope: TargetScope) -> Option<&'static str> {
    let action = &request.action;
    let element_action = matches!(
        action.action.as_str(),
        "press" | "set_value" | "perform_action"
    );
    let has_references =
        action.snapshot_id.is_some() || action.element_id.is_some() || action.ax_action.is_some();
    if !element_action && !has_references && action.observation.is_none() {
        return None;
    }
    if scope != TargetScope::AppWindow || !cfg!(target_os = "macos") {
        return Some("AX operations require a macOS application window");
    }
    if let Some(observation) = action.observation.as_deref() {
        if action.action != "observe" || !matches!(observation, "auto" | "ax" | "screenshot") {
            return Some(
                "observation is only valid for observe and must be auto, ax or screenshot",
            );
        }
    }
    let Some(owner) = &request.owner else {
        return Some("AX operations require a session owner supplied by the host channel");
    };
    if [&owner.host_id, &owner.connection_id, &owner.session_id]
        .iter()
        .any(|v| v.is_empty() || v.len() > 256)
        || owner
            .agent_id
            .as_ref()
            .is_some_and(|v| v.is_empty() || v.len() > 256)
    {
        return Some("Invalid AX session owner");
    }
    if !element_action {
        return has_references.then_some("Element references are only valid for element actions");
    }
    if [
        action.window_id.as_deref(),
        action.snapshot_id.as_deref(),
        action.element_id.as_deref(),
    ]
    .iter()
    .any(|v| v.is_none_or(|v| v.is_empty() || v.len() > 256))
    {
        return Some("Element actions require window_id, snapshot_id and element_id");
    }
    if action.x.is_some()
        || action.y.is_some()
        || action.to_x.is_some()
        || action.to_y.is_some()
        || action.button.is_some()
        || action.keys.is_some()
        || action.delta_x.is_some()
        || action.delta_y.is_some()
    {
        return Some("Element actions cannot contain coordinate or keyboard input");
    }
    if action.action == "set_value" && action.text.as_ref().is_none_or(|v| v.len() > 65536) {
        return Some("set_value requires text of at most 65536 UTF-8 bytes");
    }
    if action.action != "set_value" && action.text.is_some() {
        return Some("Only set_value accepts text among element actions");
    }
    if action.action != "perform_action" && action.ax_action.is_some() {
        return Some("ax_action is only valid for perform_action");
    }
    if action.action == "perform_action"
        && action
            .ax_action
            .as_ref()
            .is_none_or(|v| v.is_empty() || v.len() > 128)
    {
        return Some("perform_action requires an advertised ax_action");
    }
    None
}

#[tauri::command]
pub async fn computer_use_release_ax(
    host_id: String,
    connection_id: String,
    session_id: Option<String>,
    agent_id: Option<String>,
    all_agents: bool,
) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    tauri::async_runtime::spawn_blocking(move || {
        mac_accessibility::release(
            &host_id,
            &connection_id,
            session_id.as_deref(),
            agent_id.as_deref(),
            all_agents,
        )
    })
    .await
    .map_err(|error| error.to_string())?;
    #[cfg(not(target_os = "macos"))]
    let _ = (host_id, connection_id, session_id, agent_id, all_agents);
    Ok(())
}

#[tauri::command]
pub async fn computer_use_execute(request: ExecuteRequest) -> Result<Value, String> {
    let queued = std::time::Instant::now();
    tauri::async_runtime::spawn_blocking(move || {
        let queue_ms = queued.elapsed().as_secs_f64() * 1000.0;
        execute(request).map(|mut result| {
            result["timings_ms"]["worker_queue"] = json!(queue_ms);
            result
        })
    })
    .await
    .map_err(|error| format!("Computer Use worker failed: {error}"))?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_requests_allow_foreground_and_explicit_strict_requests_block_focus() {
        let default_request = serde_json::from_value::<ExecuteRequest>(json!({
            "action": {"action": "wait"}
        }))
        .unwrap();
        assert_eq!(
            default_request.delivery_policy,
            DeliveryPolicy::AllowForeground
        );

        // Focus is rejected by policy before window resolution or native input.
        let result = execute(
            serde_json::from_value(json!({
                "target_scope": "app_window", "delivery_policy": "strict_background",
                "action": {"action": "focus_window"},
            }))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(result["error_code"], "background_delivery_unsupported");
        assert_eq!(result["action_dispatched"], false);
        assert_eq!(result["retry_safe"], true);
        assert!(serde_json::from_value::<ExecuteRequest>(json!({
            "action": {"action": "click", "delivery_policy": "allow_foreground"}
        }))
        .is_err());
        assert!(serde_json::from_value::<ExecuteRequest>(json!({
            "delivery_policy": "automatic", "action": {"action": "wait"}
        }))
        .is_err());
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn windows_focus_selection_preserves_window_identity() {
        let windows = vec![
            json!({"id": "725632", "app_name": "Windows 资源管理器", "title": "PRTSNote - 文件资源管理器"}),
            json!({"id": "2229786", "app_name": "Windows 资源管理器", "title": "desktop - 文件资源管理器"}),
        ];
        assert_eq!(
            select_focus_window(&windows, Some("725632"), None),
            Ok(725632)
        );
        // An explicit ID takes precedence, even if text names a different window.
        assert_eq!(
            select_focus_window(&windows, Some("725632"), Some("desktop - 文件资源管理器")),
            Ok(725632)
        );
        assert_eq!(
            select_focus_window(&windows, None, Some("desktop - 文件资源管理器")),
            Ok(2229786)
        );
        assert!(
            select_focus_window(&windows, None, Some("Windows 资源管理器"))
                .unwrap_err()
                .contains("Multiple windows")
        );
        assert!(
            select_focus_window(&windows, Some("123"), Some("desktop - 文件资源管理器")).is_err()
        );
        assert!(select_focus_window(&windows, None, Some("")).is_err());
        assert!(select_focus_window(&windows, None, None).is_err());
    }

    #[cfg(target_os = "macos")]
    pub(super) struct MacInputTestHost {
        launch: std::process::Child,
        pub(super) state_path: std::path::PathBuf,
        _directory: tempfile::TempDir,
    }

    #[cfg(target_os = "macos")]
    impl MacInputTestHost {
        fn start() -> Self {
            Self::start_fixture("tests/fixtures/scroll_host.swift", &[])
        }

        pub(super) fn start_fixture(source: &str, args: &[&str]) -> Self {
            let directory = tempfile::tempdir().unwrap();
            let bundle = directory.path().join("InputFixture.app");
            let binaries = bundle.join("Contents/MacOS");
            std::fs::create_dir_all(&binaries).unwrap();
            std::fs::write(
                bundle.join("Contents/Info.plist"),
                r#"<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>input-fixture</string>
<key>CFBundleIdentifier</key><string>io.crabcode.input-test</string>
<key>CFBundleName</key><string>CrabCode input fixture</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>"#,
            )
            .unwrap();
            let status = Command::new("xcrun")
                .args(["swiftc", source, "-o"])
                .arg(binaries.join("input-fixture"))
                .status()
                .unwrap();
            assert!(status.success());
            let state_path = directory.path().join("state.json");
            // A bare AppKit executable activates asynchronously at launch.
            // Use LaunchServices' background flag so activation assertions
            // measure the input path, not the fixture's startup sequence.
            let launch = Command::new("open")
                .args(["-g", "-n", "-W"])
                .arg(&bundle)
                .arg("--args")
                .arg(&state_path)
                .args(args)
                .spawn()
                .unwrap();
            Self {
                launch,
                state_path,
                _directory: directory,
            }
        }

        pub(super) fn state(&self) -> Option<Value> {
            serde_json::from_slice(&std::fs::read(&self.state_path).ok()?).ok()
        }
    }

    #[cfg(target_os = "macos")]
    impl Drop for MacInputTestHost {
        fn drop(&mut self) {
            if let Some(pid) = self.state().and_then(|state| state["pid"].as_i64()) {
                // This PID is published by our isolated, temporary fixture.
                unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
            }
            let _ = self.launch.kill();
            let _ = self.launch.wait();
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn background_click_preparation_reports_each_failure_stage() {
        let action = serde_json::from_value(json!({"action": "click"})).unwrap();
        let target = WindowTarget {
            window_id: 1,
            pid: 2,
            x: 0,
            y: 0,
            width: 100,
            height: 100,
        };
        for (stage, name, code) in [
            (
                ClickFailureStage::TargetVisibility,
                "target_visibility",
                "background_click_target_offscreen",
            ),
            (
                ClickFailureStage::WindowPreparation,
                "window_preparation",
                "background_click_window_preparation_failed",
            ),
            (
                ClickFailureStage::Activation,
                "activation",
                "background_click_activation_failed",
            ),
            (
                ClickFailureStage::LayoutValidation,
                "layout_validation",
                "background_click_layout_validation_failed",
            ),
            (
                ClickFailureStage::BaselineCapture,
                "baseline_capture",
                "background_click_baseline_capture_failed",
            ),
        ] {
            let result = failed_background_click_preparation(
                &action,
                target,
                target,
                (10, 10),
                stage.failure("Native error detail".to_string()),
            );
            assert_eq!(result["failure_stage"], name);
            assert_eq!(result["error_code"], code);
            assert_eq!(result["error"], "Native error detail");
            assert_eq!(result["ok"], false);
            assert_eq!(result["dispatch_status"], "not_sent");
            assert_eq!(result["effect_status"], "not_checked");
            assert_eq!(result["verification_method"], "not_performed");
            assert!(result["summary"].as_str().unwrap().contains("no "));
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn background_click_unavailable_verification_preserves_dispatch() {
        for dispatched in [true, false] {
            let mut result = json!({});
            record_click_outcome(&mut result, dispatched, None);
            assert_eq!(result["ok"], dispatched);
            assert_eq!(
                result["dispatch_status"],
                if dispatched { "sent" } else { "uncertain" }
            );
            assert_eq!(result["effect_status"], "unavailable");
            assert_eq!(result["effect_verified"], false);
            assert_eq!(result["verification_method"], "unavailable");
            assert!(result["verification_warning"].is_string());
            if dispatched {
                assert_eq!(
                    result["summary"],
                    "Click dispatched; effect verification is unavailable"
                );
                assert!(result["error_code"].is_null());
            } else {
                assert_eq!(result["failure_stage"], "accessibility_dispatch");
            }
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn background_click_receipt_separates_dispatch_effect_and_focus() {
        for verification in [
            Ok(false),
            Ok(true),
            Err("foreground probe failed".to_string()),
        ] {
            for (dispatched, changed) in
                [(true, true), (true, false), (false, true), (false, false)]
            {
                let mut result = json!({});
                record_click_outcome(&mut result, dispatched, Some(changed));
                record_click_foreground_change(&mut result, verification.clone());
                assert_eq!(result["ok"], dispatched);
                assert_eq!(result["effect_verified"], changed);
                assert_eq!(result["visual_change_detected"], changed);
                assert_eq!(result["dispatch_succeeded"], dispatched);
                assert_eq!(
                    result["dispatch_status"],
                    if dispatched { "sent" } else { "uncertain" }
                );
                assert_eq!(
                    result["effect_status"],
                    if changed {
                        "change_detected"
                    } else {
                        "no_change_detected"
                    }
                );
                assert_eq!(result["verification_warning"].is_string(), !changed);
                if dispatched {
                    assert_eq!(
                        result["summary"],
                        if changed {
                            "Click dispatched"
                        } else {
                            "Click dispatched; effect is unverified"
                        }
                    );
                    assert!(result["error_code"].is_null());
                    assert!(result["error"].is_null());
                } else {
                    assert_eq!(result["error_code"], "background_click_dispatch_unverified");
                }
                match &verification {
                    Ok(activated) => assert_eq!(result["foreground_activated"], *activated),
                    Err(error) => {
                        assert!(result["foreground_activated"].is_null());
                        assert_eq!(result["foreground_warning"], *error);
                    }
                }
            }
        }
    }

    #[test]
    fn parses_common_shortcut_keys() {
        assert_eq!(key_from_name("ctrl").unwrap(), Key::Control);
        assert_eq!(key_from_name("Enter").unwrap(), Key::Return);
        assert_eq!(key_from_name("x").unwrap(), Key::Unicode('x'));
        assert!(key_from_name("not-a-key").is_err());
    }

    #[test]
    fn rejects_unknown_mouse_button() {
        assert!(mouse_button(Some("sideways")).is_err());
    }

    #[test]
    fn scroll_rejects_noops_unpaired_coordinates_and_excessive_deltas() {
        for value in [
            json!({"action": "scroll"}),
            json!({"action": "scroll", "delta_y": 0}),
            json!({"action": "scroll", "x": 100, "delta_y": 800}),
            json!({"action": "scroll", "delta_y": i32::MIN}),
            json!({"action": "scroll", "delta_y": MAX_SCROLL_DELTA + 1}),
        ] {
            let action: ComputerAction = serde_json::from_value(value).unwrap();
            assert!(scroll_delta(&action).is_err());
        }
        let action: ComputerAction = serde_json::from_value(json!({
            "action": "scroll", "x": 1400, "y": 700, "delta_y": -800
        }))
        .unwrap();
        assert_eq!(scroll_delta(&action).unwrap(), (0, -800));
        let mut result = json!({"ok": true});
        add_scroll_receipt(&mut result, &action);
        assert_eq!(result["ok"], true);
        assert_eq!(result["effect_verified"], false);
        assert_eq!(result["scroll"]["delta_y"], -800);
    }

    #[test]
    fn background_coordinates_are_window_local_and_translate_to_desktop_space() {
        let target = WindowTarget {
            window_id: 107,
            pid: 1084,
            x: 0,
            y: 33,
            width: 1492,
            height: 868,
        };
        let action: ComputerAction = serde_json::from_value(json!({
            "action": "click",
            "window_id": "107",
            "x": 212,
            "y": 810,
        }))
        .unwrap();
        assert_eq!(background_local_point(&action, target).unwrap(), (212, 810));
        assert_eq!(background_point(&action, target).unwrap(), (212, 843));

        for (x, y) in [(-1, 0), (0, -1), (1492, 0), (0, 868)] {
            let action: ComputerAction = serde_json::from_value(json!({
                "action": "click",
                "window_id": "107",
                "x": x,
                "y": y,
            }))
            .unwrap();
            assert!(background_point(&action, target).is_err());
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn ax_zoom_maps_screenshot_points_into_the_content_tree() {
        for (x, y) in [(0, 30), (320, 240), (-1600, -900)] {
            let target = WindowTarget {
                window_id: 42,
                pid: 123,
                x,
                y,
                width: 2560,
                height: 1320,
            };
            for zoom in [0.9_f64, 1.1, 1.25] {
                let content = CGRect::new(
                    &CGPoint::new((f64::from(x) / zoom).round(), (f64::from(y) / zoom).round()),
                    &CGSize::new((2560.0 / zoom).round(), (1320.0 / zoom).round()),
                );
                let transform = MacAxCoordinateTransform::from_window_content(target, content)
                    .expect("uniform app zoom should be calibrated");
                let point =
                    transform.point(CGPoint::new(f64::from(x) + 300.0, f64::from(y) + 690.0));
                assert!((point.x - (f64::from(x) + 300.0) / zoom).abs() < 1.0);
                assert!((point.y - (f64::from(y) + 690.0) / zoom).abs() < 1.0);
            }
        }

        let target = WindowTarget {
            window_id: 42,
            pid: 123,
            x: 0,
            y: 30,
            width: 2560,
            height: 1318,
        };
        let transform = MacAxCoordinateTransform::from_window_content(
            target,
            CGRect::new(&CGPoint::new(0.0, 33.0), &CGSize::new(2844.0, 1464.0)),
        )
        .unwrap();
        let point = transform.point(CGPoint::new(300.0, 720.0));
        let phone = CGRect::new(&CGPoint::new(262.0, 753.0), &CGSize::new(147.0, 116.0));
        let notes = CGRect::new(&CGPoint::new(262.0, 623.0), &CGSize::new(147.0, 116.0));
        assert!(phone.contains(&point));
        assert!(!notes.contains(&point));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn ax_zoom_rejects_native_content_insets_and_unrelated_geometry() {
        let target = WindowTarget {
            window_id: 42,
            pid: 123,
            x: 120,
            y: 200,
            width: 640,
            height: 442,
        };
        for (x, y, width, height) in [
            (120.0, 200.0, 640.0, 442.0),  // No zoom, including Retina screens.
            (120.0, 222.0, 640.0, 420.0),  // Native content minus title bar.
            (120.0, 200.0, 700.0, 442.0),  // One dimension changed.
            (120.0, 200.0, 640.0, 4000.0), // Scroll document.
            (120.0, 200.0, 320.0, 221.0),  // Smaller panel at unscaled origin.
            (120.0, 200.0, 0.0, 0.0),
            (f64::NAN, 200.0, 640.0, 442.0),
            (120.0, 200.0, f64::INFINITY, 442.0),
        ] {
            assert!(MacAxCoordinateTransform::from_window_content(
                target,
                CGRect::new(&CGPoint::new(x, y), &CGSize::new(width, height)),
            )
            .is_none());
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires macOS Accessibility; launches isolated windows at multiple AX zoom factors"]
    fn macos_scaled_ax_clicks_keep_native_chrome_and_content_separate() {
        mac_require_input_permission().unwrap();
        for zoom in ["0.9", "1.0", "1.1"] {
            let host =
                MacInputTestHost::start_fixture("tests/fixtures/scaled_ax_host.swift", &[zoom]);
            let initial = (0..100)
                .find_map(|_| {
                    let state = host.state();
                    if state.is_none() {
                        thread::sleep(Duration::from_millis(50));
                    }
                    state
                })
                .expect("AX fixture did not start");
            let target =
                window_target(&initial["window_id"].as_u64().unwrap().to_string()).unwrap();
            let application =
                unsafe { CFType::wrap_under_create_rule(AXUIElementCreateApplication(target.pid)) };
            let window = mac_ax_window(&application, target).unwrap();
            assert_eq!(
                mac_ax_scaled_content(&window, target).is_some(),
                zoom != "1.0",
                "zoom={zoom}"
            );
            for (field, expected) in [("upper_point", "Upper"), ("lower_point", "Lower")] {
                let point = &initial[field];
                let (x, y) = (
                    point[0].as_f64().unwrap().round() as i32,
                    point[1].as_f64().unwrap().round() as i32,
                );
                let hit = mac_ax_click_target(&application, target, x, y).unwrap();
                assert_eq!(
                    mac_ax_string(&hit, "AXTitle").as_deref(),
                    Some(expected),
                    "zoom={zoom}"
                );
                assert!(
                    matches!(
                        mac_ax_press(target, x, y),
                        MacAxPressOutcome::Performed { .. }
                    ),
                    "zoom={zoom}"
                );
            }
            // Native traffic lights are outside the scaled subtree. Resolve
            // the close button without pressing it or closing the fixture.
            let close = &initial["close_point"];
            let hit = mac_ax_click_target(
                &application,
                target,
                close[0].as_f64().unwrap().round() as i32,
                close[1].as_f64().unwrap().round() as i32,
            )
            .unwrap();
            assert_eq!(
                mac_ax_string(&hit, "AXSubrole").as_deref(),
                Some("AXCloseButton"),
                "zoom={zoom}"
            );
            let after = (0..40)
                .find_map(|_| {
                    let state = host.state()?;
                    if state["upper_presses"] == 1 && state["lower_presses"] == 1 {
                        Some(state)
                    } else {
                        thread::sleep(Duration::from_millis(50));
                        None
                    }
                })
                .expect("AX clicks did not reach the intended buttons exactly once");
            assert_eq!(after["upper_presses"], 1);
            assert_eq!(after["lower_presses"], 1);
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "read-only live AX hit test; set CRABCODE_TEST_WINDOW_ID, X, Y and EXPECT_AX_LABEL"]
    fn macos_ax_click_target_is_read_only() {
        let window_id = std::env::var("CRABCODE_TEST_WINDOW_ID").unwrap();
        let x = std::env::var("CRABCODE_TEST_X")
            .unwrap()
            .parse::<i32>()
            .unwrap();
        let y = std::env::var("CRABCODE_TEST_Y")
            .unwrap()
            .parse::<i32>()
            .unwrap();
        let expected = std::env::var("CRABCODE_TEST_EXPECT_AX_LABEL").unwrap();
        let target = window_target(&window_id).unwrap();
        let application =
            unsafe { CFType::wrap_under_create_rule(AXUIElementCreateApplication(target.pid)) };
        let window = mac_ax_window(&application, target).unwrap();
        eprintln!(
            "calibrated={}",
            mac_ax_scaled_content(&window, target).is_some()
        );
        let hit = mac_ax_click_target(&application, target, x, y).unwrap();
        let labels = ["AXTitle", "AXDescription", "AXSubrole"]
            .map(|attribute| mac_ax_string(&hit, attribute).unwrap_or_default());
        assert_eq!(mac_ax_window_id(&hit), Ok(target.window_id));
        assert!(
            labels.iter().any(|label| label == &expected),
            "hit {labels:?}, expected {expected}"
        );
        eprintln!("hit={labels:?}");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn window_group_excludes_sibling_documents_and_unproven_overlays() {
        let root = WindowTarget {
            window_id: 1,
            pid: 10,
            x: 0,
            y: 0,
            width: 500,
            height: 500,
        };
        let info = |id, on_screen| MacWindowInfo {
            target: WindowTarget {
                window_id: id,
                ..root
            },
            app_name: String::new(),
            title: String::new(),
            layer: 0,
            on_screen,
        };
        let windows = vec![info(2, true), info(3, true), info(4, false), info(1, true)];
        let mut relations = WindowRelations::default();
        relations.record(1, WindowKind::Document, None);
        relations.record(2, WindowKind::Document, None);
        relations.record(3, WindowKind::Auxiliary, None);
        relations.record(4, WindowKind::Sheet, Some((1, "AXSheets")));
        let group = mac_window_group_from_info(root, &windows, &relations);
        assert_eq!(
            group
                .components
                .iter()
                .map(|w| w.target.window_id)
                .collect::<Vec<_>>(),
            vec![1]
        );
        assert_eq!(group.excluded.len(), 2);
        assert!(mac_event_target_from_group(group, root, (100, 100)).is_err());
        // A positively identified sibling cannot become the dispatch target,
        // even when it has the same title, PID, layer and rectangle.
        let group = mac_window_group_from_info(root, &[info(2, true), info(1, true)], &relations);
        assert_eq!(
            mac_event_target_from_group(group, root, (100, 100))
                .unwrap()
                .target
                .window_id,
            1
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn window_group_routes_proven_auxiliaries_but_not_passive_companions() {
        let root = WindowTarget {
            window_id: 1,
            pid: 10,
            x: 0,
            y: 0,
            width: 500,
            height: 500,
        };
        let info = |id, on_screen| MacWindowInfo {
            target: WindowTarget {
                window_id: id,
                ..root
            },
            app_name: String::new(),
            title: String::new(),
            layer: 3,
            on_screen,
        };
        let windows = vec![info(4, false), info(2, true), info(3, true), info(1, true)];
        let mut relations = WindowRelations::default();
        relations.record(1, WindowKind::Document, None);
        relations.record(2, WindowKind::Passive, Some((1, "AXParent")));
        relations.record(3, WindowKind::Sheet, Some((1, "AXSheets")));
        relations.record(4, WindowKind::Popover, Some((1, "AXParent")));
        let group = mac_window_group_from_info(root, &windows, &relations);
        assert_eq!(
            group
                .components
                .iter()
                .map(|w| w.target.window_id)
                .collect::<Vec<_>>(),
            vec![2, 3, 1]
        );
        assert_eq!(
            mac_event_target_from_group(group, root, (100, 100))
                .unwrap()
                .target
                .window_id,
            3
        );
        let group = mac_window_group_from_info(root, &windows, &WindowRelations::default());
        assert_eq!(group.components.len(), 1);
        assert!(mac_event_target_from_group(group, root, (100, 100)).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn keyboard_rejection_and_partial_dispatch_have_distinct_receipts() {
        let receipt = json!({"requested_window_id": "1", "resolved_window_id": "3", "focus_changed_by_tool": false});
        let rejected = keyboard_failure(
            receipt.clone(),
            "keyboard_focus_unresolved",
            "no ownership",
            0,
        );
        assert_eq!(rejected["action_dispatched"], false);
        assert_eq!(rejected["focus_changed_by_tool"], false);
        assert_eq!(rejected["retry_safe"], true);
        let partial =
            keyboard_failure(receipt, "keyboard_dispatch_interrupted", "focus changed", 2);
        assert_eq!(partial["action_dispatched"], true);
        assert_eq!(partial["retry_safe"], false);
        assert_eq!(partial["keyboard_events_sent"], 2);
        assert_eq!(partial["requires_observation"], true);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn translucent_window_pixels_are_composited_with_straight_alpha() {
        use xcap::image::Rgba;

        let mut rgba = [128, 128, 128, 128];
        mac_unpremultiply_pixel(&mut rgba);
        assert_eq!(rgba, [255, 255, 255, 128]);
        let mut root = RgbaImage::from_pixel(1, 1, Rgba([0, 0, 255, 255]));
        let overlay = RgbaImage::from_pixel(1, 1, Rgba(rgba));
        xcap::image::imageops::overlay(&mut root, &overlay, 0, 0);
        assert_eq!(root.get_pixel(0, 0).0[..3], [128, 128, 255]);
        assert!(root.get_pixel(0, 0).0[3] >= 254);
        let mut transparent = [0, 0, 0, 0];
        mac_unpremultiply_pixel(&mut transparent);
        assert_eq!(transparent, [0, 0, 0, 0]);
        let mut opaque = [23, 101, 255, 255];
        mac_unpremultiply_pixel(&mut opaque);
        assert_eq!(opaque, [23, 101, 255, 255]);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn click_verification_ignores_unrelated_window_changes() {
        use xcap::image::Rgba;

        let base = RgbaImage::from_pixel(256, 256, Rgba([0, 0, 0, 255]));
        let capture = |image| ScreenshotCapture {
            image,
            origin_x: 0,
            origin_y: 30,
            target: "window:42".to_string(),
            component_window_ids: Vec::new(),
            hidden_component_window_ids: Vec::new(),
            component_capture_errors: Vec::new(),
            window_components: Vec::new(),
            excluded_windows: Vec::new(),
        };
        let before = capture(base.clone());
        let same = capture(base.clone());
        assert!(
            !screenshot_visual_change(&before, &same, (128, 158))
                .unwrap()
                .detected
        );

        let mut unrelated_pixels = base.clone();
        for y in 0..12 {
            for x in 0..12 {
                unrelated_pixels.put_pixel(x, y, Rgba([255, 255, 255, 255]));
            }
        }
        let unrelated = capture(unrelated_pixels);
        assert!(
            !screenshot_visual_change(&before, &unrelated, (128, 158))
                .unwrap()
                .detected
        );

        let mut local_pixels = base;
        for y in 125..131 {
            for x in 125..131 {
                local_pixels.put_pixel(x, y, Rgba([255, 255, 255, 255]));
            }
        }
        let local = capture(local_pixels);
        let change = screenshot_visual_change(&before, &local, (128, 158)).unwrap();
        assert!(change.detected);
        assert_eq!(change.changed_pixels, 36);
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires macOS Accessibility and Screen Recording; launches isolated overlay windows"]
    fn macos_window_group_captures_covered_dialog_and_drops_dismissed_pixels() {
        let wait_state = |host: &MacInputTestHost, phase: &str| {
            (0..100)
                .find_map(|_| {
                    let state = host.state().filter(|state| state["phase"] == phase);
                    if state.is_none() {
                        thread::sleep(Duration::from_millis(50));
                    }
                    state
                })
                .expect("window group fixture did not reach the requested phase")
        };
        let host = MacInputTestHost::start_fixture("tests/fixtures/window_group_host.swift", &[]);
        let initial = wait_state(&host, "open");
        let root_id = initial["root_id"].as_u64().unwrap() as u32;
        let dialog_id = initial["dialog_id"].as_u64().unwrap() as u32;
        let companion_id = initial["companion_id"].as_u64().unwrap() as u32;
        let root = window_target(&root_id.to_string()).unwrap();
        let assert_frame = |dialog_visible: bool| {
            let capture = mac_capture_window_group(root).unwrap();
            assert!(capture.component_capture_errors.is_empty());
            assert!(capture.hidden_component_window_ids.is_empty());
            assert!(
                capture.component_window_ids.contains(&companion_id),
                "missing companion {companion_id}; root={root:?}; windows={:?}",
                mac_all_window_info()
                    .unwrap()
                    .into_iter()
                    .filter(|window| window.target.pid == root.pid)
                    .collect::<Vec<_>>()
            );
            assert_eq!(
                capture.component_window_ids.contains(&dialog_id),
                dialog_visible
            );
            let check_pixel = |x, y, expected: [u8; 4]| {
                let actual = capture.image.get_pixel(x, y).0;
                assert!(
                    actual.iter().zip(expected).all(|(a, b)| a.abs_diff(b) <= 3),
                    "pixel ({x}, {y}) was {actual:?}, expected {expected:?}"
                );
            };
            check_pixel(20, 20, [255, 255, 255, 255]);
            if dialog_visible {
                check_pixel(60, 60, [128, 128, 128, 255]);
                check_pixel(180, 120, [255, 255, 255, 255]);
            } else {
                check_pixel(60, 60, [0, 0, 0, 255]);
                check_pixel(180, 120, [0, 0, 0, 255]);
            }
            let target = mac_background_event_target(root, (root.x + 180, root.y + 120)).unwrap();
            assert_eq!(
                target.target.window_id,
                if dialog_visible { dialog_id } else { root_id }
            );
        };
        assert_frame(true);

        // Another process covers every target pixel. It must neither prevent
        // capture nor appear in the captured application window group.
        let occluder = MacInputTestHost::start_fixture(
            "tests/fixtures/window_group_host.swift",
            &[&companion_id.to_string()],
        );
        let occluder_state = wait_state(&occluder, "open");
        let occluder_id = occluder_state["root_id"].as_u64().unwrap() as u32;
        let windows = mac_all_window_info().unwrap();
        let index = |id| {
            windows
                .iter()
                .position(|w| w.target.window_id == id)
                .unwrap()
        };
        assert!(index(occluder_id) < index(dialog_id));
        assert_frame(true);
        for (phase, visible) in [("closed", false), ("reopened", true), ("closed", false)] {
            std::fs::write(host.state_path.with_extension("command"), phase).unwrap();
            let state = wait_state(&host, phase);
            assert_eq!(state["active"], false);
            assert_frame(visible);
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "read-only live window-group capture; set CRABCODE_TEST_WINDOW_ID explicitly"]
    fn macos_background_window_group_capture_is_read_only() {
        let window_id = std::env::var("CRABCODE_TEST_WINDOW_ID")
            .expect("CRABCODE_TEST_WINDOW_ID must name a live window");
        let target = window_target(&window_id).expect("live root window was not found");
        let process_windows = mac_all_window_info()
            .expect("window enumeration failed")
            .into_iter()
            .filter(|window| window.target.pid == target.pid)
            .collect::<Vec<_>>();
        eprintln!("process_windows={process_windows:?}");
        eprintln!(
            "window_relations={:?}",
            mac_ax_relations::snapshot(
                &mac_ax_relations::application(target.pid).unwrap(),
                target.pid
            )
            .relations
        );
        let capture = mac_capture_window_group(target).expect("window group capture failed");
        assert_eq!(capture.image.dimensions(), (target.width, target.height));
        assert_eq!(capture.origin_x, target.x);
        assert_eq!(capture.origin_y, target.y);
        assert!(capture.component_window_ids.contains(&target.window_id));
        if let Ok(expected) = std::env::var("CRABCODE_TEST_EXPECT_COMPONENT_ID") {
            let expected = expected
                .parse::<u32>()
                .expect("component id was not numeric");
            assert!(
                capture.component_window_ids.contains(&expected),
                "component {expected} missing from {:?}",
                capture.component_window_ids
            );
        }
        if let Ok(path) = std::env::var("CRABCODE_TEST_CAPTURE_OUTPUT") {
            capture
                .image
                .save(path)
                .expect("unable to save read-only capture fixture output");
        }
        eprintln!(
            "components={:?} hidden={:?} errors={:?}",
            capture.component_window_ids,
            capture.hidden_component_window_ids,
            capture.component_capture_errors
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn scroll_steps_preserve_signed_diagonal_totals_and_bound_event_size() {
        for (dx, dy) in [(0, -800), (1, 51), (-51, 13), (0, 1), (10_000, -9_999)] {
            let steps = scroll_steps(dx, dy);
            assert!(steps.len() <= 200);
            assert_eq!(steps.iter().map(|(x, _)| x).sum::<i32>(), dx);
            assert_eq!(steps.iter().map(|(_, y)| y).sum::<i32>(), dy);
            assert!(steps
                .iter()
                .all(|(x, y)| x.abs() <= SCROLL_STEP_PIXELS && y.abs() <= SCROLL_STEP_PIXELS));
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_scroll_event_preserves_window_coordinates_and_pixel_direction() {
        let target = WindowTarget {
            window_id: 14461,
            pid: 42,
            x: 0,
            y: 30,
            width: 2560,
            height: 1316,
        };
        for window_id in [14461, 151872] {
            let target = WindowTarget {
                window_id,
                ..target
            };
            let event = mac_scroll_event(Some(target), (1400, 700), 25, -50).unwrap();
            assert_eq!(
                event.get_integer_value_field(CG_EVENT_TARGET_WINDOW),
                i64::from(window_id)
            );
            assert_eq!(
                event.get_integer_value_field(CG_EVENT_RECEIVING_WINDOW),
                i64::from(window_id)
            );
            assert_eq!((event.location().x, event.location().y), (1400.0, 700.0));
            assert_eq!(
                event.get_integer_value_field(EventField::SCROLL_WHEEL_EVENT_IS_CONTINUOUS),
                1
            );
            let foreground = mac_scroll_event(None, (1400, 700), 25, -50).unwrap();
            for (axis, delta) in [
                (EventField::SCROLL_WHEEL_EVENT_POINT_DELTA_AXIS_1, 50),
                (EventField::SCROLL_WHEEL_EVENT_POINT_DELTA_AXIS_2, -25),
            ] {
                assert_eq!(event.get_integer_value_field(axis), delta);
                assert_eq!(foreground.get_integer_value_field(axis), delta);
            }
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires macOS Accessibility; launches isolated overlapping test windows"]
    fn macos_background_raw_click_hits_off_center_button() {
        mac_require_input_permission().unwrap();
        let host = MacInputTestHost::start();
        let initial = (0..100)
            .find_map(|_| {
                let state = host.state();
                if state.is_none() {
                    thread::sleep(Duration::from_millis(50));
                }
                state
            })
            .expect("pointer fixture did not become ready");
        let target = window_target(&initial["target_id"].to_string()).unwrap();
        let point = (
            initial["button_x"].as_i64().unwrap() as i32,
            initial["button_y"].as_i64().unwrap() as i32,
        );
        mac_click(target, point.0, point.1, Some("left"), 1).unwrap();
        thread::sleep(Duration::from_millis(500));
        let after = host.state().unwrap();
        eprintln!("target={target:?}, point={point:?}, state={after}");
        assert_eq!(after["button_presses"], 1);
        assert_eq!(after["decoy_button_presses"], 0);
        assert_eq!(after["target_clicks"], 0);
        assert_eq!(after["decoy_clicks"], 0);
        for received in after["received_mouse_points"].as_array().unwrap() {
            assert_eq!(received[0].as_f64().unwrap(), f64::from(target.window_id));
            assert_eq!(received[3].as_f64().unwrap(), f64::from(point.0));
            assert_eq!(received[4].as_f64().unwrap(), f64::from(point.1));
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires macOS Accessibility and Screen Recording; activates isolated test windows"]
    fn macos_background_mouse_fallback_activates_inactive_content() {
        mac_require_input_permission().unwrap();
        let host = MacInputTestHost::start_fixture(
            "tests/fixtures/scroll_host.swift",
            &["--require-active"],
        );
        let initial = (0..100)
            .find_map(|_| {
                let state = host.state();
                if state.is_none() {
                    thread::sleep(Duration::from_millis(50));
                }
                state
            })
            .expect("pointer fixture did not become ready");
        assert_eq!(initial["active"], false);
        let target = window_target(&initial["decoy_id"].to_string()).unwrap();
        let point = (
            initial["x"].as_i64().unwrap() as i32,
            initial["y"].as_i64().unwrap() as i32,
        );
        // Reproduce the failure: an ordinary PID-routed click is received by
        // the selected process/window, but inactive content discards it.
        mac_click(target, point.0, point.1, Some("left"), 1).unwrap();
        let dropped = (0..40)
            .find_map(|_| {
                let state = host.state()?;
                if state["decoy_dropped_clicks"] != 0 || state["decoy_clicks"] != 0 {
                    Some(state)
                } else {
                    thread::sleep(Duration::from_millis(50));
                    None
                }
            })
            .unwrap_or_else(|| host.state().unwrap());
        assert_eq!(
            dropped["decoy_dropped_clicks"], 1,
            "initial={initial}; target={target:?}; point={point:?}; after={dropped}"
        );
        assert_eq!(dropped["decoy_clicks"], 0);

        let result = execute(
            serde_json::from_value(json!({
                "mode": "background_app", "delivery_policy": "allow_foreground",
                "action": {
                    "action": "click", "window_id": target.window_id.to_string(),
                    "x": point.0 - target.x, "y": point.1 - target.y,
                    "include_screenshot": false,
                },
            }))
            .unwrap(),
        )
        .unwrap();
        let after = host.state().unwrap();
        assert_eq!(result["input_method"], "quartz_event", "{result}");
        assert_eq!(result["dispatch_succeeded"], true, "{result}");
        assert_eq!(result["dispatch_window_id"], target.window_id.to_string());
        assert_eq!(after["decoy_clicks"], 1, "{after}");
        assert_eq!(after["decoy_dropped_clicks"], 1, "{after}");
        assert_eq!(after["target_clicks"], 0);
        assert_eq!(after["modifiers"], json!([]));
        // A moved observation or a vanished target must not pass preflight.
        let stale = WindowTarget {
            x: target.x + 1,
            ..target
        };
        assert!(mac_validate_mouse_layout(stale, target, point).is_err());
        let missing = WindowTarget {
            window_id: u32::MAX,
            ..target
        };
        assert!(mac_validate_mouse_layout(target, missing, point).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "sends one live click; explicitly set CRABCODE_TEST_WINDOW_ID, X, Y and CAPTURE_OUTPUT"]
    fn macos_background_click_at_explicit_live_point() {
        let window_id = std::env::var("CRABCODE_TEST_WINDOW_ID").unwrap();
        let x = std::env::var("CRABCODE_TEST_X")
            .unwrap()
            .parse::<i32>()
            .unwrap();
        let y = std::env::var("CRABCODE_TEST_Y")
            .unwrap()
            .parse::<i32>()
            .unwrap();
        let output = std::env::var("CRABCODE_TEST_CAPTURE_OUTPUT").unwrap();
        let result = execute(
            serde_json::from_value(json!({
                "mode": "background_app", "delivery_policy": "allow_foreground",
                "action": {
                    "action": "click", "window_id": window_id, "x": x, "y": y,
                },
            }))
            .unwrap(),
        )
        .unwrap();
        let mut receipt = result.clone();
        receipt.as_object_mut().unwrap().remove("screenshot");
        eprintln!("{receipt}");
        if let Some(encoded) = result["screenshot"]["data"].as_str() {
            std::fs::write(output, STANDARD.decode(encoded).unwrap()).unwrap();
        }
        assert_eq!(result["dispatch_succeeded"], true, "{receipt}");
        // Delivery into the real app is judged from the saved screenshot,
        // not inferred from the dispatch receipt or unrelated pixel changes.
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires macOS Accessibility permission and launches isolated overlapping test windows"]
    fn macos_background_pointer_targets_one_of_two_windows_without_focus() {
        mac_require_input_permission()
            .expect("grant Accessibility to the test runner before running this ignored test");
        let host = MacInputTestHost::start();
        let read_state = || host.state();
        let mut initial = None;
        for _ in 0..100 {
            initial = read_state();
            if initial.is_some() {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let initial = initial.expect("scroll host did not become ready");
        assert_eq!(initial["active"], false);
        let target = WindowTarget {
            window_id: initial["target_id"].as_u64().unwrap() as u32,
            pid: initial["pid"].as_i64().unwrap() as i32,
            x: initial["origin_x"].as_i64().unwrap() as i32,
            y: initial["origin_y"].as_i64().unwrap() as i32,
            width: 0,
            height: 0,
        };
        let point = (
            initial["x"].as_i64().unwrap() as i32,
            initial["y"].as_i64().unwrap() as i32,
        );
        mac_scroll(Some(target), point, 0, 300).unwrap();
        thread::sleep(Duration::from_millis(500));
        let after = read_state().unwrap();
        assert!(
            after["target_offset"].as_f64().unwrap() > initial["target_offset"].as_f64().unwrap(),
            "target did not scroll: {after}"
        );
        assert_eq!(after["decoy_offset"], initial["decoy_offset"]);
        assert_eq!(after["decoy_events"], 0);
        assert_eq!(after["active"], false);
        let assert_desktop_preserved = |after: &Value| {
            assert_eq!(
                after["active"], false,
                "target state was not restored: {after}"
            );
            assert_eq!(
                after["ever_frontmost"], false,
                "target stole foreground: {after}"
            );
            assert_eq!(
                after["ever_raised"], false,
                "target window was raised: {after}"
            );
            // Keep strict cursor/focus checks for idle desktop runs, while also
            // allowing this regression to run alongside real user activity.
            if std::env::var_os("CRABCODE_TEST_ALLOW_USER_INPUT").is_none() {
                assert_eq!(after["cursor"], initial["cursor"]);
                assert_eq!(after["frontmost_pid"], initial["frontmost_pid"]);
            }
        };
        assert_desktop_preserved(&after);

        let foreground_monitor = MacForegroundMonitor::start(target.pid).unwrap();
        mac_click(target, point.0, point.1, Some("left"), 1).unwrap();
        thread::sleep(Duration::from_millis(500));
        assert!(!foreground_monitor.finish().unwrap());
        let after = read_state().unwrap();
        assert_eq!(
            after["target_clicks"], 1,
            "click did not reach target: {after}"
        );
        assert_eq!(after["decoy_clicks"], 0);
        assert_eq!(after["modifiers"], json!([0]));
        assert_desktop_preserved(&after);

        mac_click(target, point.0, point.1, Some("left"), 2).unwrap();
        mac_click(target, point.0, point.1, Some("right"), 1).unwrap();
        mac_click(target, point.0, point.1, Some("middle"), 1).unwrap();
        mac_drag(
            target,
            point,
            (point.0 + 40, point.1 + 20),
            Some("left"),
            100,
        )
        .unwrap();
        thread::sleep(Duration::from_millis(500));
        let after = read_state().unwrap();
        assert_eq!(after["target_clicks"], 4, "missing clicks: {after}");
        assert_eq!(after["click_counts"], json!([1, 1, 2, 1]));
        let click_times = after["click_times"].as_array().unwrap();
        assert!(click_times[2].as_f64().unwrap() - click_times[1].as_f64().unwrap() >= 0.08);
        assert_eq!(after["target_other_clicks"], 2);
        assert!(after["target_drags"].as_u64().unwrap() > 0);
        assert_eq!(after["modifiers"], json!([0, 0, 0, 0]));
        for field in ["decoy_clicks", "decoy_other_clicks", "decoy_drags"] {
            assert_eq!(after[field], 0, "input reached decoy: {after}");
        }
        assert_desktop_preserved(&after);

        let button_point = (
            initial["button_x"].as_i64().unwrap() as i32,
            initial["button_y"].as_i64().unwrap() as i32,
        );
        mac_click(target, button_point.0, button_point.1, Some("left"), 1).unwrap();
        thread::sleep(Duration::from_millis(500));
        let after = read_state().unwrap();
        assert_eq!(
            after["button_presses"], 1,
            "native button did not activate: {after}"
        );
        assert_desktop_preserved(&after);

        let decoy_target = WindowTarget {
            window_id: initial["decoy_id"].as_u64().unwrap() as u32,
            ..target
        };
        let decoy_button_point = (
            initial["decoy_button_x"].as_i64().unwrap() as i32,
            initial["decoy_button_y"].as_i64().unwrap() as i32,
        );
        match mac_ax_press(decoy_target, decoy_button_point.0, decoy_button_point.1) {
            MacAxPressOutcome::Performed { action } => assert_eq!(action, "AXPress"),
            outcome => panic!("AXPress did not activate the native button: {outcome:?}"),
        }
        thread::sleep(Duration::from_millis(500));
        let after = read_state().unwrap();
        assert_eq!(
            after["decoy_button_presses"], 1,
            "AXPress did not activate native button: {after}"
        );
        assert_desktop_preserved(&after);

        // The production path must not emit any application-activation record.
        thread::sleep(Duration::from_millis(200));
        assert_desktop_preserved(&read_state().unwrap());
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires macOS Accessibility and Screen Recording; launches isolated click test windows"]
    fn macos_background_click_allows_activation_and_mouse_fallback() {
        mac_require_input_permission()
            .expect("grant Accessibility to the test runner before running this ignored test");
        let host = MacInputTestHost::start();
        let read_state = || host.state();
        let mut initial = None;
        for _ in 0..100 {
            initial = read_state();
            if initial.is_some() {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let initial = initial.expect("scroll host did not become ready");
        assert_eq!(initial["active"], false);
        let target = WindowTarget {
            window_id: initial["target_id"].as_u64().unwrap() as u32,
            pid: initial["pid"].as_i64().unwrap() as i32,
            x: initial["origin_x"].as_i64().unwrap() as i32,
            y: initial["origin_y"].as_i64().unwrap() as i32,
            width: 0,
            height: 0,
        };
        let point = (
            initial["x"].as_i64().unwrap() as i32,
            initial["y"].as_i64().unwrap() as i32,
        );

        let assert_desktop_preserved = |after: &Value| {
            assert_eq!(
                after["active"], false,
                "target state was not restored: {after}"
            );
            assert_eq!(
                after["ever_frontmost"], false,
                "target stole foreground: {after}"
            );
            assert_eq!(
                after["ever_raised"], false,
                "target window was raised: {after}"
            );
            // Keep strict cursor/focus checks for idle desktop runs, while also
            // allowing this regression to run alongside real user activity.
            if std::env::var_os("CRABCODE_TEST_ALLOW_USER_INPUT").is_none() {
                assert_eq!(after["cursor"], initial["cursor"]);
                assert_eq!(after["frontmost_pid"], initial["frontmost_pid"]);
            }
        };

        let button_point = (
            initial["button_x"].as_i64().unwrap() as i32,
            initial["button_y"].as_i64().unwrap() as i32,
        );
        let decoy_target = WindowTarget {
            window_id: initial["decoy_id"].as_u64().unwrap() as u32,
            ..target
        };
        let decoy_button_point = (
            initial["decoy_button_x"].as_i64().unwrap() as i32,
            initial["decoy_button_y"].as_i64().unwrap() as i32,
        );
        // The app-wide AX hit test sees the overlapping decoy. Resolve the
        // covered target's own AX button instead of falling back to a raw click.
        let foreground_monitor = MacForegroundMonitor::start(target.pid).unwrap();
        match mac_ax_press(target, button_point.0, button_point.1) {
            MacAxPressOutcome::Performed { action } => assert_eq!(action, "AXPress"),
            outcome => panic!("window-scoped AXPress did not find the covered button: {outcome:?}"),
        }
        thread::sleep(Duration::from_millis(500));
        assert!(!foreground_monitor.finish().unwrap());
        let after = read_state().unwrap();
        assert_eq!(after["button_presses"], 1);
        assert_eq!(after["decoy_button_presses"], 0);
        assert_desktop_preserved(&after);

        // Exercise the actual ComputerUse entry point, including observation,
        // semantic dispatch and focus diagnostics.
        let click = json!({
            "action": "click",
            "window_id": decoy_target.window_id.to_string(),
            "x": decoy_button_point.0 - target.x,
            "y": decoy_button_point.1 - target.y,
            "include_screenshot": false,
        });
        let result = execute(
            serde_json::from_value(json!({
                "mode": "background_app", "delivery_policy": "allow_foreground", "action": click,
            }))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(result["input_method"], "accessibility_action", "{result}");
        let after_click = read_state().unwrap();
        assert_eq!(
            after_click["decoy_button_presses"], 1,
            "{result}; {after_click}"
        );
        // An occluded AppKit window can retain stale screenshot pixels. The
        // fixture's action counter independently verifies delivery; the host
        // reports successful dispatch with a warning if pixels have not changed.
        assert_eq!(result["dispatch_succeeded"], true, "{result}");
        assert_eq!(result["ok"], true, "{result}");
        assert!(
            result["summary"]
                .as_str()
                .unwrap()
                .starts_with("Click dispatched"),
            "{result}"
        );
        assert!(result["error_code"].is_null(), "{result}");
        if result["visual_change_detected"] == false {
            assert_eq!(result["effect_verified"], false, "{result}");
            assert!(result["verification_warning"].is_string(), "{result}");
        }
        assert_eq!(result["foreground_activated"], false, "{result}");
        assert_desktop_preserved(&after_click);

        // AXPress may legitimately activate the app. The result must preserve
        // the observed effect instead of reporting a foreground violation.
        let mut activating_click = click.clone();
        activating_click["x"] =
            json!(initial["activating_button_x"].as_i64().unwrap() - i64::from(target.x));
        activating_click["y"] =
            json!(initial["activating_button_y"].as_i64().unwrap() - i64::from(target.y));
        let result = execute(
            serde_json::from_value(json!({
                "mode": "background_app", "delivery_policy": "allow_foreground", "action": activating_click,
            }))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(result["input_method"], "accessibility_action", "{result}");
        assert_eq!(result["dispatch_succeeded"], true, "{result}");
        let after_activation = read_state().unwrap();
        assert_eq!(
            result["foreground_activated"], true,
            "{result}; {after_activation}"
        );
        assert_eq!(result["ok"], true, "{result}");
        assert!(result["error_code"].is_null(), "{result}");
        assert_eq!(
            after_activation["activating_button_presses"], 1,
            "{after_activation}"
        );
        assert_eq!(
            after_activation["ever_frontmost"], true,
            "{after_activation}"
        );

        let mut custom_point = click.clone();
        custom_point["x"] = json!(point.0 - target.x);
        custom_point["y"] = json!(point.1 - target.y);
        let mut double_click = custom_point.clone();
        double_click["action"] = json!("double_click");
        let mut right_click = custom_point.clone();
        right_click["button"] = json!("right");
        let mut middle_click = custom_point.clone();
        middle_click["button"] = json!("middle");
        for (action, clicks, other_clicks) in [
            (custom_point, 1, 0),
            (double_click, 3, 0),
            (right_click, 3, 1),
            (middle_click, 3, 2),
        ] {
            let result = execute(
                serde_json::from_value(json!({
                    "mode": "background_app", "delivery_policy": "allow_foreground", "action": action,
                }))
                .unwrap(),
            )
            .unwrap();
            assert_eq!(result["dispatch_succeeded"], true, "{result}");
            assert_eq!(result["input_method"], "quartz_event", "{result}");
            assert_eq!(result["ok"], true, "{result}");
            assert!(result["error_code"].is_null(), "{result}");
            let after = read_state().unwrap();
            assert_eq!(after["decoy_clicks"], clicks, "{result}; {after}");
            assert_eq!(
                after["decoy_other_clicks"], other_clicks,
                "{result}; {after}"
            );
            assert_eq!(after["target_clicks"], 0, "{after}");
            assert_eq!(after["target_other_clicks"], 0, "{after}");
        }
        let focus = execute(
            serde_json::from_value(json!({
                "mode": "background_app", "delivery_policy": "allow_foreground", "action": {
                    "action": "focus_window", "window_id": decoy_target.window_id.to_string(),
                    "include_screenshot": false,
                },
            }))
            .unwrap(),
        )
        .unwrap();
        assert_eq!(focus["ok"], true, "{focus}");
    }

    #[test]
    fn missing_input_permission_keeps_graphical_computer_use_available() {
        let (gui_available, input_available, reason) =
            capability_status(None, Some("accessibility permission denied".to_string()));
        assert!(gui_available);
        assert!(!input_available);
        assert_eq!(reason.as_deref(), Some("accessibility permission denied"));
    }

    #[test]
    fn computer_use_mode_uses_foreground_default_and_rejects_unknown_values() {
        let request: ExecuteRequest = serde_json::from_value(json!({
            "action": { "action": "list_windows" }
        }))
        .unwrap();
        assert_eq!(request.mode, None);
        assert_eq!(request.delivery_policy, DeliveryPolicy::AllowForeground);
        assert!(serde_json::from_value::<ExecuteRequest>(json!({
            "mode": "automatic",
            "action": { "action": "list_windows" }
        }))
        .is_err());
    }

    #[test]
    fn background_mode_rejects_full_desktop_actions_without_fallback() {
        let request: ExecuteRequest = serde_json::from_value(json!({
            "mode": "background_app",
            "action": { "action": "list_displays" }
        }))
        .unwrap();
        let result = execute(request).unwrap();
        assert_eq!(result["ok"], false);
        assert_eq!(result["action_dispatched"], false);
        let error = result["error"].as_str().unwrap();
        assert!(
            error.contains("does not expose the full desktop")
                || error.contains("background_app mode is unavailable")
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn mac_keyboard_shortcuts_preserve_command_and_validate_before_focus() {
        let (code, flags) = mac_key_combination(&["CMD".into(), "C".into()]).unwrap();
        assert_eq!(code, KeyCode::ANSI_C);
        assert_eq!(flags, CGEventFlags::CGEventFlagCommand);
        let (_, flags) =
            mac_key_combination(&["command".into(), "shift".into(), "N".into()]).unwrap();
        assert_eq!(
            flags,
            CGEventFlags::CGEventFlagCommand | CGEventFlags::CGEventFlagShift
        );
        assert!(mac_key_combination(&[]).is_err());
        assert!(mac_key_combination(&["CMD".into()]).is_err());
        assert!(mac_key_combination(&["C".into(), "V".into()]).is_err());
        assert!(mac_key_combination(&["unknown".into()]).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires Accessibility; activates only isolated keyboard fixture windows"]
    fn macos_keyboard_routes_windows_and_name_panel() {
        mac_require_input_permission().expect("Accessibility permission required");
        let host = MacInputTestHost::start_fixture("tests/fixtures/keyboard_host.swift", &[]);
        let wait = |field: &str, expected: Value| {
            for _ in 0..100 {
                if let Some(state) = host.state() {
                    if state[field] == expected {
                        return state;
                    }
                }
                thread::sleep(Duration::from_millis(50));
            }
            panic!(
                "fixture did not reach {field}={expected}: {:?}",
                host.state()
            );
        };
        let state = wait("phase", json!("ready"));
        let send = |id: &Value, action: &str, value: Value| {
            let mut request =
                json!({"action": action, "window_id": id.to_string(), "include_screenshot": false});
            if action == "type" {
                request["text"] = value;
            } else if action == "keypress" {
                request["keys"] = value;
            }
            let result = execute_background(ExecuteRequest {
                mode: Some(ComputerUseMode::BackgroundApp),
                target_scope: Some(TargetScope::AppWindow),
                delivery_policy: DeliveryPolicy::AllowForeground,
                owner: None,
                action: serde_json::from_value(request).unwrap(),
            })
            .unwrap();
            assert_eq!(
                result["ok"],
                true,
                "{action}: {result}; fixture={:?}",
                host.state()
            );
        };
        send(&state["second"], "focus_window", Value::Null);
        send(&state["first"], "focus_window", Value::Null);
        send(&state["first"], "keypress", json!(["CMD", "A"]));
        send(&state["first"], "type", json!("report"));
        let after = wait("first_text", json!("report"));
        assert_eq!(after["second_text"], "second");
        // Typing collapses selection. AppKit disables Copy for an empty
        // selection, so explicitly select the text before testing the shortcut.
        send(&state["first"], "keypress", json!(["CMD", "A"]));
        send(&state["first"], "keypress", json!(["CMD", "C"]));
        wait("copies", json!(1));
        std::fs::write(host.state_path.with_extension("command"), "panel").unwrap();
        let panel = wait("phase", json!("panel"));
        // makeKeyAndOrderFront publishes its window asynchronously. The old
        // repeated enumeration was slow enough to mask this fixture race.
        let panel_id = panel["panel"].to_string();
        (0..100)
            .find_map(|_| {
                let target = window_target(&panel_id).ok();
                if target.is_none() {
                    thread::sleep(Duration::from_millis(20));
                }
                target
            })
            .expect("fixture panel was not published by WindowServer");
        send(&panel["panel"], "type", json!("report.txt"));
        wait("name", json!("report.txt"));
        send(&panel["panel"], "keypress", json!(["CMD", "A"]));
        send(&panel["panel"], "type", json!("backup.txt"));
        wait("name", json!("backup.txt"));
        let unrelated = window_target(&state["second"].to_string()).unwrap();
        let before = mac_front_process_serial_number().unwrap();
        assert!(mac_prepare_keyboard(unrelated, DeliveryPolicy::StrictBackground).is_err());
        assert_eq!(before, mac_front_process_serial_number().unwrap());
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "launches an isolated TextEdit process to verify real background window input"]
    fn macos_background_window_round_trip_does_not_use_foreground_input() {
        use std::fs;
        use std::io::Write as _;

        let permission_settings = Settings {
            open_prompt_to_get_permissions: false,
            ..Settings::default()
        };
        if Enigo::new(&permission_settings).is_err() {
            eprintln!("skipped: the test binary does not have macOS Accessibility permission");
            return;
        }

        let mut file = tempfile::Builder::new().suffix(".txt").tempfile().unwrap();
        file.write_all(b"before").unwrap();
        file.as_file_mut().sync_all().unwrap();
        let path = file.path().to_path_buf();
        let filename = path.file_name().unwrap().to_string_lossy().to_string();

        let status = Command::new("open")
            .args(["-n", "-g", "-a", "TextEdit"])
            .arg(&path)
            .status()
            .unwrap();
        assert!(status.success());

        let mut target: Option<(String, WindowTarget)> = None;
        for _ in 0..50 {
            if let Some(found) = Window::all().unwrap().into_iter().find(|window| {
                window
                    .title()
                    .map(|title| title.contains(&filename))
                    .unwrap_or(false)
            }) {
                let id = found.id().unwrap().to_string();
                target = Some((id.clone(), window_target(&id).unwrap()));
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
        let (window_id, target) = target.expect("isolated TextEdit window did not appear");

        let observe: ComputerAction = serde_json::from_value(json!({
            "action": "observe",
            "window_id": window_id,
        }))
        .unwrap();
        assert!(screenshot(&observe).is_ok());

        let x = target.x + target.width as i32 / 2;
        let y = target.y + target.height as i32 / 2;
        mac_click(target, x, y, Some("left"), 1).unwrap();
        mac_press_keys(target.pid, &["CMD".to_string(), "A".to_string()]).unwrap();
        mac_type_text(target.pid, "background-app-round-trip").unwrap();
        mac_press_keys(target.pid, &["CMD".to_string(), "S".to_string()]).unwrap();

        let mut saved = String::new();
        for _ in 0..30 {
            saved = fs::read_to_string(&path).unwrap_or_default();
            if saved.contains("background-app-round-trip") {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
        let _ = Command::new("kill").arg(target.pid.to_string()).status();
        assert_eq!(saved, "background-app-round-trip");
    }
}
