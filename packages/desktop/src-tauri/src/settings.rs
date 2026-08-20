use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

const KEYRING_SERVICE: &str = "io.crabcode.desktop";

fn settings_path() -> Result<PathBuf, String> {
    let home =
        dirs::home_dir().ok_or_else(|| "Unable to locate the user home directory".to_string())?;
    Ok(home.join(".crabcode").join("settings_desktop.json"))
}

fn default_settings() -> Value {
    json!({
        "schema_version": 2,
        "active_connection_id": "local",
        "connection_order": ["local"],
        "connections": [{
            "id": "local",
            "name": "Local",
            "base_url": "http://127.0.0.1:4096",
            "credential_ref": null,
            "allow_insecure_remote": false,
            "projects": [],
            "last_project_path": null,
            "last_project_id": null
        }],
        "python_path": null,
        "sidebar_width": 280
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
