use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(not(target_os = "macos"))]
use tauri::Manager;

const KEYRING_SERVICE: &str = "io.crabcode.desktop";

fn settings_path() -> Result<PathBuf, String> {
    let home =
        dirs::home_dir().ok_or_else(|| "Unable to locate the user home directory".to_string())?;
    Ok(home.join(".crabcode").join("settings_desktop.json"))
}

fn custom_dock_icon_path() -> Result<PathBuf, String> {
    let home =
        dirs::home_dir().ok_or_else(|| "Unable to locate the user home directory".to_string())?;
    Ok(home.join(".crabcode").join("dock_icon_custom.png"))
}

fn dock_icon_bytes(choice: &str) -> Result<Vec<u8>, String> {
    match choice {
        "dark" => Ok(include_bytes!("../icons/icon.png").to_vec()),
        "light" => Ok(include_bytes!("../resources/dock-icon-light.png").to_vec()),
        "custom" => fs::read(custom_dock_icon_path()?)
            .map_err(|error| format!("Unable to read the custom Dock icon: {error}")),
        _ => Err("Unknown Dock icon choice".to_string()),
    }
}

fn default_settings() -> Value {
    json!({
        "schema_version": 4,
        "active_connection_id": "local",
        "connection_order": ["local"],
        "connections": [{
            "id": "local",
            "name": "Local",
            "base_url": "http://127.0.0.1:4096",
            "credential_ref": null,
            "allow_insecure_remote": false,
            "last_model_profile": null,
            "document_workspace_root": null,
            "projects": [],
            "favorite_items": [],
            "last_project_path": null,
            "last_project_id": null
        }],
        "python_path": null,
        "sidebar_width": 280,
        "project_files_width": 640,
        "project_files_max_tabs": 5,
        "document_agent_width": 400,
        "document_agent_collapsed": false,
        "document_show_original_text": false,
        "document_translation_concurrency": 3,
        "document_translation_batch_size": 200,
        "theme_mode": "system",
        "active_theme_id": "builtin.crab",
        "custom_theme_presets": [],
        "pointer_cursor": true,
        "ui_font_size": 14,
        "code_font_size": 12,
        "diff_marker_style": "color",
        "font_smoothing": true,
        "show_turn_duration": true,
        "turn_duration_format": "hms",
        "composer_send_key": "enter",
        "file_upload_mode": "content",
        "file_upload_max_size_mb": 5,
        "dock_icon": "dark"
    })
}

fn contains_secret(value: &Value) -> bool {
    match value {
        Value::Object(values) => values.iter().any(|(key, child)| {
            matches!(key.as_str(), "password" | "token" | "access_token" | "jwt")
                || contains_secret(child)
        }),
        Value::Array(values) => values.iter().any(contains_secret),
        _ => false,
    }
}

#[tauri::command]
pub fn load_desktop_settings() -> Result<Value, String> {
    let path = settings_path()?;
    if !path.exists() {
        return Ok(default_settings());
    }
    let raw = fs::read_to_string(&path)
        .map_err(|error| format!("Unable to read desktop settings: {error}"))?;
    match serde_json::from_str::<Value>(&raw) {
        Ok(value) if value.is_object() => Ok(value),
        _ => {
            let timestamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|value| value.as_secs())
                .unwrap_or(0);
            let backup = path.with_file_name(format!("settings_desktop.corrupt-{timestamp}.json"));
            fs::rename(&path, &backup).map_err(|error| {
                format!("Desktop settings are invalid and could not be backed up: {error}")
            })?;
            Ok(default_settings())
        }
    }
}

#[tauri::command]
pub fn save_desktop_settings(settings: Value) -> Result<(), String> {
    if !settings.is_object() {
        return Err("Desktop settings must be a JSON object".to_string());
    }
    if contains_secret(&settings) {
        return Err("Desktop settings cannot contain passwords or access tokens".to_string());
    }
    let path = settings_path()?;
    let parent = path
        .parent()
        .ok_or_else(|| "Invalid desktop settings path".to_string())?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("Unable to create settings directory: {error}"))?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)
        .map_err(|error| format!("Unable to create temporary settings file: {error}"))?;
    let content = serde_json::to_vec_pretty(&settings)
        .map_err(|error| format!("Unable to serialize desktop settings: {error}"))?;
    temporary
        .write_all(&content)
        .and_then(|_| temporary.write_all(b"\n"))
        .and_then(|_| temporary.as_file_mut().sync_all())
        .map_err(|error| format!("Unable to write desktop settings: {error}"))?;
    temporary
        .persist(&path)
        .map_err(|error| format!("Unable to replace desktop settings: {}", error.error))?;
    Ok(())
}

fn safe_export_filename(filename: &str) -> bool {
    let stem = filename
        .split('.')
        .next()
        .unwrap_or_default()
        .trim_end_matches([' ', '.'])
        .to_ascii_uppercase();
    let reserved = matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || (stem.len() == 4
            && (stem.starts_with("COM") || stem.starts_with("LPT"))
            && matches!(stem.as_bytes()[3], b'1'..=b'9'));
    !filename.is_empty()
        && filename.len() <= 160
        && !filename.contains(['/', '\\', '\0', '<', '>', ':', '"', '|', '?', '*'])
        && !filename.ends_with([' ', '.'])
        && !reserved
        && Path::new(filename)
            .file_name()
            .and_then(|value| value.to_str())
            == Some(filename)
        && (filename.ends_with(".crabtheme.json") || filename.ends_with(".crabskin"))
}

#[tauri::command]
pub fn save_theme_export(filename: String, bytes: Vec<u8>) -> Result<String, String> {
    if !safe_export_filename(&filename) {
        return Err("Theme export filename is invalid".to_string());
    }
    if bytes.is_empty() || bytes.len() > 12 * 1024 * 1024 {
        return Err("Theme export must be between 1 byte and 12MB".to_string());
    }
    let directory = dirs::download_dir()
        .or_else(dirs::home_dir)
        .ok_or_else(|| "Unable to locate a Downloads directory".to_string())?;
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Unable to create the export directory: {error}"))?;
    let suffix = if filename.ends_with(".crabtheme.json") {
        ".crabtheme.json"
    } else {
        ".crabskin"
    };
    let stem = filename
        .strip_suffix(suffix)
        .ok_or_else(|| "Theme export filename is invalid".to_string())?;
    let mut destination = directory.join(&filename);
    for index in 1..10_000 {
        if !destination.exists() {
            break;
        }
        destination = directory.join(format!("{stem}-{index}{suffix}"));
    }
    if destination.exists() {
        return Err("Unable to allocate a unique theme export filename".to_string());
    }
    let mut temporary = tempfile::NamedTempFile::new_in(&directory)
        .map_err(|error| format!("Unable to create a temporary export: {error}"))?;
    temporary
        .write_all(&bytes)
        .and_then(|_| temporary.as_file_mut().sync_all())
        .map_err(|error| format!("Unable to write the theme export: {error}"))?;
    temporary
        .persist(&destination)
        .map_err(|error| format!("Unable to save the theme export: {}", error.error))?;
    Ok(destination.to_string_lossy().into_owned())
}

#[tauri::command]
pub fn set_dock_icon(
    app: tauri::AppHandle,
    choice: String,
    png_bytes: Option<Vec<u8>>,
) -> Result<(), String> {
    if let ("custom", Some(bytes)) = (choice.as_str(), png_bytes) {
        if bytes.len() > 5 * 1024 * 1024 {
            return Err("Custom Dock icon cannot exceed 5MB".to_string());
        }
        tauri::image::Image::from_bytes(&bytes)
            .map_err(|error| format!("Unable to decode the custom Dock icon: {error}"))?;
        let path = custom_dock_icon_path()?;
        let parent = path
            .parent()
            .ok_or_else(|| "Invalid custom Dock icon path".to_string())?;
        fs::create_dir_all(parent)
            .map_err(|error| format!("Unable to create settings directory: {error}"))?;
        fs::write(&path, &bytes)
            .map_err(|error| format!("Unable to save the custom Dock icon: {error}"))?;
    }
    let bytes = dock_icon_bytes(&choice)?;
    tauri::image::Image::from_bytes(&bytes)
        .map_err(|error| format!("Unable to decode the Dock icon: {error}"))?;
    #[cfg(target_os = "macos")]
    {
        app.run_on_main_thread(move || {
            use objc2::{AllocAnyThread, MainThreadMarker};
            use objc2_app_kit::{NSApplication, NSImage};
            use objc2_foundation::NSData;
            let marker = unsafe { MainThreadMarker::new_unchecked() };
            let application = NSApplication::sharedApplication(marker);
            let data = NSData::with_bytes(&bytes);
            if let Some(image) = NSImage::initWithData(NSImage::alloc(), &data) {
                unsafe {
                    application.setApplicationIconImage(Some(&image));
                }
            }
        })
        .map_err(|error| format!("Unable to apply the Dock icon: {error}"))
    }
    #[cfg(not(target_os = "macos"))]
    {
        let icon = tauri::image::Image::from_bytes(&bytes)
            .map_err(|error| format!("Unable to decode the application icon: {error}"))?;
        let app_for_main = app.clone();
        app.run_on_main_thread(move || {
            if let Some(window) = app_for_main.get_webview_window("main") {
                let _ = window.set_icon(icon);
            }
        })
        .map_err(|error| format!("Unable to apply the application icon: {error}"))
    }
}

#[tauri::command]
pub fn load_custom_dock_icon() -> Result<Option<Vec<u8>>, String> {
    let path = custom_dock_icon_path()?;
    if !path.exists() {
        return Ok(None);
    }
    fs::read(path)
        .map(Some)
        .map_err(|error| format!("Unable to read the custom Dock icon: {error}"))
}

#[tauri::command]
pub fn store_credential(credential_ref: String, password: String) -> Result<(), String> {
    if credential_ref.trim().is_empty() || password.is_empty() {
        return Err("Credential reference and password are required".to_string());
    }
    keyring::Entry::new(KEYRING_SERVICE, &credential_ref)
        .map_err(|error| format!("Unable to open the system credential store: {error}"))?
        .set_password(&password)
        .map_err(|error| format!("Unable to save the credential: {error}"))
}

#[tauri::command]
pub fn delete_credential(credential_ref: String) -> Result<(), String> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, &credential_ref)
        .map_err(|error| format!("Unable to open the system credential store: {error}"))?;
    match entry.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(error) => Err(format!("Unable to delete the credential: {error}")),
    }
}

pub fn read_credential(credential_ref: &str) -> Result<String, String> {
    keyring::Entry::new(KEYRING_SERVICE, credential_ref)
        .map_err(|error| format!("Unable to open the system credential store: {error}"))?
        .get_password()
        .map_err(|error| match error {
            keyring::Error::NoEntry => {
                "No saved password is available for this connection".to_string()
            }
            other => format!("Unable to read the credential: {other}"),
        })
}

#[cfg(test)]
mod tests {
    use super::{default_settings, safe_export_filename};

    #[cfg(target_os = "windows")]
    #[test]
    #[ignore = "writes a temporary Windows Credential Manager entry; run explicitly"]
    fn windows_credentials_survive_a_new_entry() {
        let key = format!("audit-{}-{:?}", std::process::id(), std::time::SystemTime::now());
        let first = keyring::Entry::new("crabcode-windows-regression", &key).unwrap();
        first.set_password("test-only-not-a-real-secret").unwrap();
        let read = keyring::Entry::new("crabcode-windows-regression", &key)
            .unwrap().get_password();
        first.delete_credential().unwrap();
        assert_eq!(read.unwrap(), "test-only-not-a-real-secret");
    }

    #[test]
    fn project_file_tabs_default_to_five() {
        assert_eq!(default_settings()["project_files_max_tabs"], 5);
    }

    #[test]
    fn theme_export_filenames_are_basenames_with_known_suffixes() {
        assert!(safe_export_filename("深海.crabskin"));
        assert!(safe_export_filename("graphite.crabtheme.json"));
        assert!(!safe_export_filename("../escape.crabskin"));
        assert!(!safe_export_filename("nested/theme.crabtheme.json"));
        assert!(!safe_export_filename("bad:name.crabskin"));
        assert!(!safe_export_filename("CON.crabskin"));
        assert!(!safe_export_filename("lpt9.crabtheme.json"));
        assert!(!safe_export_filename("theme.zip"));
    }
}
