use crate::settings::read_credential;
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{self, BufRead, BufReader, Read};
use std::net::{IpAddr, ToSocketAddrs};
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use url::Url;

const MIN_PYTHON_MAJOR: u32 = 3;
const MIN_PYTHON_MINOR: u32 = 10;
const GATEWAY_PROTOCOL: i64 = 1;
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

fn configure_python_utf8(command: &mut Command) {
    command
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8");
}

fn stop_child_tree(child: &mut Child) -> io::Result<()> {
    if child.try_wait()?.is_some() {
        return Ok(());
    }

    #[cfg(target_os = "windows")]
    {
        let pid = child.id().to_string();
        let status = Command::new("taskkill")
            .args(["/PID", pid.as_str(), "/T", "/F"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW)
            .status();
        if status.is_ok_and(|value| value.success()) {
            let _ = child.wait();
            return Ok(());
        }
    }

    if child.try_wait()?.is_none() {
        child.kill()?;
        let _ = child.wait();
    }
    Ok(())
}

#[derive(Default)]
pub struct GatewayProcesses(Mutex<HashMap<String, Child>>);

impl GatewayProcesses {
    pub fn stop_all(&self) {
        if let Ok(mut processes) = self.0.lock() {
            for (_, mut child) in processes.drain() {
                let _ = stop_child_tree(&mut child);
            }
        }
    }
}

#[derive(Serialize)]
pub struct AuthResult {
    access_token: Option<String>,
    expires_in: u64,
    mode: String,
}

#[derive(Deserialize)]
struct AuthInfo {
    mode: String,
    methods: Vec<String>,
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    expires_in: u64,
}

#[derive(Serialize)]
pub struct EnsureGatewayResult {
    ready: bool,
    started_by_desktop: bool,
    python: Option<String>,
    version: Option<String>,
    message: String,
}

fn client() -> Result<Client, String> {
    Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .map_err(|error| format!("Unable to create HTTP client: {error}"))
}

fn parse_base_url(value: &str) -> Result<Url, String> {
    let mut url = Url::parse(value).map_err(|error| format!("Invalid Gateway URL: {error}"))?;
    if !matches!(url.scheme(), "http" | "https") {
        return Err("Gateway URL must use http:// or https://".to_string());
    }
    if url.host_str().is_none() {
        return Err("Gateway URL must include a host".to_string());
    }
    url.set_query(None);
    url.set_fragment(None);
    if !url.path().ends_with('/') {
        let next = format!("{}/", url.path().trim_end_matches('/'));
        url.set_path(&next);
    }
    Ok(url)
}

fn endpoint(base: &Url, path: &str) -> Result<Url, String> {
    base.join(path.trim_start_matches('/'))
        .map_err(|error| format!("Unable to construct Gateway URL: {error}"))
}

#[tauri::command]
pub fn authenticate_connection(
    base_url: String,
    credential_ref: Option<String>,
) -> Result<AuthResult, String> {
    let base = parse_base_url(&base_url)?;
    let http = client()?;
    let info: AuthInfo = http
        .get(endpoint(&base, "auth/info")?)
        .send()
        .map_err(|error| format!("Unable to reach Gateway authentication endpoint: {error}"))?
        .error_for_status()
        .map_err(|error| format!("Gateway authentication discovery failed: {error}"))?
        .json()
        .map_err(|error| format!("Gateway returned invalid authentication metadata: {error}"))?;

    if info.mode == "none" {
        return Ok(AuthResult {
            access_token: None,
            expires_in: 0,
            mode: info.mode,
        });
    }
    if !info.methods.iter().any(|method| method == "password") {
        return Err("This Gateway does not support password authentication".to_string());
    }
    let reference = credential_ref
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| "This Gateway requires a saved password".to_string())?;
    let password = read_credential(&reference)?;
    let token: TokenResponse = http
        .post(endpoint(&base, "auth/token")?)
        .json(&json!({"grant_type": "password", "password": password}))
        .send()
        .map_err(|error| format!("Unable to authenticate with Gateway: {error}"))?
        .error_for_status()
        .map_err(|error| format!("Gateway rejected the password: {error}"))?
        .json()
        .map_err(|error| format!("Gateway returned an invalid token response: {error}"))?;
    Ok(AuthResult {
        access_token: Some(token.access_token),
        expires_in: token.expires_in,
        mode: info.mode,
    })
}

fn is_loopback(base: &Url) -> bool {
    let Some(host) = base.host_str() else {
        return false;
    };
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    if host
        .parse::<IpAddr>()
        .is_ok_and(|address| address.is_loopback())
    {
        return true;
    }
    let port = base.port_or_known_default().unwrap_or(80);
    (host, port)
        .to_socket_addrs()
        .map(|addresses| {
            addresses
                .into_iter()
                .all(|address| address.ip().is_loopback())
        })
        .unwrap_or(false)
}

fn probe_health(base: &Url, credential_ref: Option<&str>) -> Result<Option<Value>, String> {
    let http = client()?;
    let mut request = http.get(endpoint(base, "health")?);
    if let Some(reference) = credential_ref {
        if let Ok(password) = read_credential(reference) {
            request = request.bearer_auth(password);
        }
    }
    let response = match request.send() {
        Ok(response) => response,
        Err(error) if error.is_connect() || error.is_timeout() => return Ok(None),
        Err(error) => return Err(format!("Gateway health check failed: {error}")),
    };
    if response.status().as_u16() == 401 {
        return Ok(Some(
            json!({"status": "authenticated", "version": null, "protocol_version": GATEWAY_PROTOCOL}),
        ));
    }
    let response = response
        .error_for_status()
        .map_err(|error| format!("Gateway health check failed: {error}"))?;
    let value: Value = response.json().map_err(|error| {
        format!("The service at this address is not a CrabCode Gateway: {error}")
    })?;
    if value.get("status").and_then(Value::as_str).is_none()
        || value.get("version").and_then(Value::as_str).is_none()
    {
        return Err("The health endpoint is not a CrabCode Gateway".to_string());
    }
    let min = value
        .get("min_protocol_version")
        .and_then(Value::as_i64)
        .or_else(|| value.get("protocol_version").and_then(Value::as_i64));
    let max = value
        .get("max_protocol_version")
        .and_then(Value::as_i64)
        .or_else(|| value.get("protocol_version").and_then(Value::as_i64));
    if !matches!((min, max), (Some(low), Some(high)) if low <= GATEWAY_PROTOCOL && high >= GATEWAY_PROTOCOL)
    {
        return Err("The running Gateway does not support protocol v1".to_string());
    }
    Ok(Some(value))
}

fn python_version(candidate: &str) -> Option<(u32, u32)> {
    let output = Command::new(candidate).arg("--version").output().ok()?;
    if !output.status.success() {
        return None;
    }
    let raw = if output.stdout.is_empty() {
        String::from_utf8_lossy(&output.stderr)
    } else {
        String::from_utf8_lossy(&output.stdout)
    };
    let version = raw.split_whitespace().find(|part| {
        part.chars()
            .next()
            .is_some_and(|character| character.is_ascii_digit())
    })?;
    let mut parts = version.split('.');
    Some((parts.next()?.parse().ok()?, parts.next()?.parse().ok()?))
}

fn detect_python(configured: Option<&str>) -> Result<String, String> {
    let mut candidates = Vec::new();
    if let Some(value) = configured.filter(|value| !value.trim().is_empty()) {
        candidates.push(value.to_string());
    }
    candidates.extend(["python3".to_string(), "python".to_string()]);
    #[cfg(target_os = "windows")]
    candidates.push("py".to_string());
    for candidate in candidates {
        if let Some((major, minor)) = python_version(&candidate) {
            if major > MIN_PYTHON_MAJOR || (major == MIN_PYTHON_MAJOR && minor >= MIN_PYTHON_MINOR)
            {
                return Ok(candidate);
            }
        }
    }
    Err("Python 3.10 or newer was not found. Install Python or set a Python path in Desktop settings.".to_string())
}

fn detect_document_engine_python(configured: Option<&str>) -> Result<String, String> {
    let mut candidates = Vec::new();
    if let Some(value) = configured.filter(|value| !value.trim().is_empty()) {
        candidates.push(value.to_string());
    }
    candidates.extend(["python3".to_string(), "python".to_string()]);
    #[cfg(target_os = "windows")]
    candidates.push("py".to_string());
    for candidate in candidates {
        if python_version(&candidate)
            .is_some_and(|(major, minor)| major == 3 && (10..=13).contains(&minor))
        {
            return Ok(candidate);
        }
    }
    Err("The high-fidelity PDF engine requires Python 3.10 through 3.13.".to_string())
}

fn run_document_engine_command(
    python_path: Option<&str>,
    arguments: &[&str],
) -> Result<Value, String> {
    let python = detect_document_engine_python(python_path)?;
    let mut command = Command::new(&python);
    configure_python_utf8(&mut command);
    let output = command
        .args(["-m", "crabcode_cli", "document-engine"])
        .args(arguments)
        .output()
        .map_err(|error| format!("Unable to start document engine manager: {error}"))?;
    parse_document_engine_output(
        output.status.success(),
        &String::from_utf8_lossy(&output.stdout),
        &String::from_utf8_lossy(&output.stderr),
    )
}

fn parse_document_engine_output(
    succeeded: bool,
    stdout: &str,
    stderr: &str,
) -> Result<Value, String> {
    let parsed = serde_json::from_str::<Value>(stdout.trim()).ok();
    if succeeded {
        return parsed.ok_or_else(|| "Document engine manager returned invalid JSON.".to_string());
    }
    if let Some(detail) = parsed
        .as_ref()
        .and_then(|value| value.get("detail"))
        .and_then(Value::as_str)
    {
        return Err(detail.to_string());
    }
    let stderr = stderr.trim().to_string();
    Err(if stderr.is_empty() {
        "Document engine manager failed.".to_string()
    } else {
        stderr
    })
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DocumentEngineInstallProgress {
    operation_id: String,
    stage: String,
    detail: String,
    percent: u8,
}

fn install_progress(operation_id: &str, detail: &str) -> DocumentEngineInstallProgress {
    let (stage, percent) = if detail.contains("启用") {
        ("activating", 96)
    } else if detail.contains("下载并校验") {
        ("downloading_assets", 68)
    } else if detail.contains("校验") {
        ("verifying", 88)
    } else if detail.contains("安装程序与依赖") || detail.contains("正在安装") {
        ("installing", 36)
    } else if detail.contains("创建独立 Python 环境") {
        ("creating_environment", 15)
    } else {
        ("preparing", 5)
    };
    DocumentEngineInstallProgress {
        operation_id: operation_id.to_string(),
        stage: stage.to_string(),
        detail: detail.to_string(),
        percent,
    }
}

fn run_document_engine_install_command(
    app: AppHandle,
    operation_id: String,
    python_path: Option<String>,
    bundle: Option<String>,
) -> Result<Value, String> {
    let python = detect_document_engine_python(python_path.as_deref())?;
    let mut command = Command::new(&python);
    configure_python_utf8(&mut command);
    command
        .args(["-m", "crabcode_cli", "document-engine", "install", "--json"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(path) = bundle.as_deref().filter(|value| !value.trim().is_empty()) {
        command.args(["--bundle", path]);
    }

    let mut child = command
        .spawn()
        .map_err(|error| format!("Unable to start document engine manager: {error}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Unable to read document engine manager output.".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "Unable to read document engine manager progress.".to_string())?;

    let stdout_thread = thread::spawn(move || {
        let mut output = String::new();
        let mut reader = BufReader::new(stdout);
        reader
            .read_to_string(&mut output)
            .map(|_| output)
            .map_err(|error| format!("Unable to read document engine manager output: {error}"))
    });
    let progress_app = app.clone();
    let progress_operation_id = operation_id.clone();
    let stderr_thread = thread::spawn(move || {
        let mut captured = Vec::new();
        for line in BufReader::new(stderr).lines() {
            let line = line.map_err(|error| {
                format!("Unable to read document engine manager progress: {error}")
            })?;
            if line.starts_with("正在") {
                let _ = progress_app.emit(
                    "document-engine-install-progress",
                    install_progress(&progress_operation_id, &line),
                );
            }
            captured.push(line);
        }
        Ok::<String, String>(captured.join("\n"))
    });

    let status = child
        .wait()
        .map_err(|error| format!("Unable to wait for document engine manager: {error}"))?;
    let stdout = stdout_thread
        .join()
        .map_err(|_| "Document engine output reader stopped unexpectedly.".to_string())??;
    let stderr = stderr_thread
        .join()
        .map_err(|_| "Document engine progress reader stopped unexpectedly.".to_string())??;
    let result = parse_document_engine_output(status.success(), &stdout, &stderr);
    if result.is_ok() {
        let _ = app.emit(
            "document-engine-install-progress",
            DocumentEngineInstallProgress {
                operation_id,
                stage: "complete".to_string(),
                detail: "高精度 PDF 引擎安装完成".to_string(),
                percent: 100,
            },
        );
    }
    result
}

#[tauri::command]
pub fn document_engine_status(python_path: Option<String>) -> Result<Value, String> {
    run_document_engine_command(python_path.as_deref(), &["status", "--json"])
}

#[tauri::command]
pub async fn install_document_engine(
    app: AppHandle,
    python_path: Option<String>,
    bundle: Option<String>,
    operation_id: String,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run_document_engine_install_command(app, operation_id, python_path, bundle)
    })
    .await
    .map_err(|error| format!("Document engine installer task failed: {error}"))?
}

#[tauri::command]
pub fn remove_document_engine(python_path: Option<String>) -> Result<Value, String> {
    run_document_engine_command(python_path.as_deref(), &["remove", "--yes", "--json"])
}

fn installed_gateway_version(python: &str) -> Option<String> {
    let script = "import crabcode_gateway; print(getattr(crabcode_gateway, '__version__', ''))";
    let output = Command::new(python).args(["-c", script]).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!version.is_empty()).then_some(version)
}

fn install_gateway(python: &str) -> Result<(), String> {
    let package = format!("crabcode[gateway]=={}", env!("CARGO_PKG_VERSION"));
    let output = Command::new(python)
        .args(["-m", "pip", "install", "--upgrade", &package])
        .output()
        .map_err(|error| format!("Unable to start pip: {error}"))?;
    if output.status.success() {
        return Ok(());
    }
    let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
    Err(format!(
        "Failed to install {package}. Run `{python} -m pip install --upgrade \"{package}\"` manually. {detail}"
    ))
}

#[tauri::command]
pub fn ensure_local_gateway(
    processes: tauri::State<'_, GatewayProcesses>,
    connection_id: String,
    base_url: String,
    python_path: Option<String>,
    credential_ref: Option<String>,
) -> Result<EnsureGatewayResult, String> {
    let base = parse_base_url(&base_url)?;
    if !is_loopback(&base) {
        return Err(
            "Desktop may only install and start Gateway processes for loopback addresses"
                .to_string(),
        );
    }
    if let Some(health) = probe_health(&base, credential_ref.as_deref())? {
        return Ok(EnsureGatewayResult {
            ready: true,
            started_by_desktop: processes
                .0
                .lock()
                .map(|items| items.contains_key(&connection_id))
                .unwrap_or(false),
            python: None,
            version: health
                .get("version")
                .and_then(Value::as_str)
                .map(str::to_string),
            message: "Gateway is ready".to_string(),
        });
    }

    let python = detect_python(python_path.as_deref())?;
    if installed_gateway_version(&python).as_deref() != Some(env!("CARGO_PKG_VERSION")) {
        install_gateway(&python)?;
    }
    let host = base.host_str().unwrap_or("127.0.0.1");
    let port = base.port_or_known_default().unwrap_or(4096).to_string();
    let mut command = Command::new(&python);
    command.args([
        "-m",
        "crabcode_cli",
        "gateway",
        "--host",
        host,
        "--port",
        &port,
    ]);
    if let Some(home) = dirs::home_dir() {
        command.current_dir(home);
    }
    let child = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("Unable to start the local Gateway: {error}"))?;
    processes
        .0
        .lock()
        .map_err(|_| "Gateway process registry is unavailable".to_string())?
        .insert(connection_id.clone(), child);

    for _ in 0..30 {
        thread::sleep(Duration::from_millis(350));
        if let Some(health) = probe_health(&base, credential_ref.as_deref())? {
            return Ok(EnsureGatewayResult {
                ready: true,
                started_by_desktop: true,
                python: Some(python),
                version: health
                    .get("version")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                message: "Desktop started the local Gateway".to_string(),
            });
        }
        let exited = processes
            .0
            .lock()
            .map_err(|_| "Gateway process registry is unavailable".to_string())?
            .get_mut(&connection_id)
            .and_then(|process| process.try_wait().ok().flatten())
            .is_some();
        if exited {
            processes
                .0
                .lock()
                .map_err(|_| "Gateway process registry is unavailable".to_string())?
                .remove(&connection_id);
            return Err("The local Gateway process exited before becoming ready".to_string());
        }
    }
    shutdown_gateway(processes, connection_id)?;
    Err("The local Gateway did not become ready within 10 seconds".to_string())
}

#[tauri::command]
pub fn shutdown_gateway(
    processes: tauri::State<'_, GatewayProcesses>,
    connection_id: String,
) -> Result<bool, String> {
    let child = processes
        .0
        .lock()
        .map_err(|_| "Gateway process registry is unavailable".to_string())?
        .remove(&connection_id);
    let Some(mut child) = child else {
        return Ok(false);
    };
    stop_child_tree(&mut child).map_err(|error| format!("Unable to stop Gateway: {error}"))?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_loopback_addresses_are_local() {
        assert!(is_loopback(
            &parse_base_url("http://127.0.0.1:4096").unwrap()
        ));
        assert!(is_loopback(
            &parse_base_url("http://localhost:4096").unwrap()
        ));
        assert!(!is_loopback(
            &parse_base_url("https://192.0.2.1:4096").unwrap()
        ));
    }

    #[test]
    fn normalizes_base_urls() {
        assert_eq!(
            parse_base_url("https://example.com:4096").unwrap().as_str(),
            "https://example.com:4096/"
        );
        assert!(parse_base_url("ws://localhost:4096").is_err());
    }

    #[test]
    fn maps_document_engine_install_stages_to_monotonic_progress() {
        let messages = [
            "正在下载高精度 PDF 引擎",
            "正在创建独立 Python 环境",
            "正在从 BabelDOC 官方源安装程序与依赖",
            "正在下载并校验 BabelDOC 官方模型与字体",
            "正在校验高精度 PDF 引擎",
            "正在启用高精度 PDF 引擎",
        ];
        let progress = messages.map(|message| install_progress("test", message).percent);
        assert_eq!(progress, [5, 15, 36, 68, 88, 96]);
        assert!(progress.windows(2).all(|pair| pair[0] < pair[1]));
    }
}
