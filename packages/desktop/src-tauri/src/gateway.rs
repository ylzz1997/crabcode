use crate::settings::read_credential;
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom};
use std::net::{IpAddr, ToSocketAddrs};
#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager};
use url::Url;

const MIN_PYTHON_MAJOR: u32 = 3;
const MIN_PYTHON_MINOR: u32 = 10;
const GATEWAY_PROTOCOL: i64 = 1;
const DESKTOP_ORIGIN: &str = "tauri://localhost";
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

fn configure_python_utf8(command: &mut Command) {
    command
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8");
    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);
}

fn python_scripts_directory(python: &str) -> Option<PathBuf> {
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    command.args([
        "-c",
        "import sysconfig; print(sysconfig.get_path('scripts') or '')",
    ]);
    let output = run_probe_command(&mut command, Duration::from_secs(5)).ok()?;
    let path = PathBuf::from(output.lines().last()?.trim());
    (!path.as_os_str().is_empty()).then_some(path)
}

fn gateway_search_path(python: &str) -> Option<std::ffi::OsString> {
    let mut paths = Vec::new();
    if let Some(path) = python_scripts_directory(python) {
        paths.push(path);
    }
    if let Some(home) = dirs::home_dir() {
        paths.push(home.join(".cargo").join("bin"));
        paths.push(home.join(".local").join("bin"));
    }
    #[cfg(target_os = "macos")]
    for path in ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"] {
        paths.push(PathBuf::from(path));
    }
    if let Some(existing) = std::env::var_os("PATH") {
        paths.extend(std::env::split_paths(&existing));
    }
    let mut unique = Vec::new();
    for path in paths {
        if !unique.contains(&path) {
            unique.push(path);
        }
    }
    std::env::join_paths(unique).ok()
}

fn configure_gateway_command(command: &mut Command, python: &str, host: &str, port: &str) {
    configure_python_utf8(command);
    if let Some(path) = gateway_search_path(python) {
        command.env("PATH", path);
    }
    command.args([
        "-m",
        "crabcode_cli",
        "gateway",
        "--host",
        host,
        "--port",
        port,
        "--cors",
        DESKTOP_ORIGIN,
    ]);
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
pub struct GatewayProcesses {
    children: Mutex<HashMap<String, Child>>,
    startup: Mutex<()>,
    stopping: AtomicBool,
}

impl GatewayProcesses {
    pub fn stop_all(&self) {
        self.stopping.store(true, Ordering::Release);
        if let Ok(mut processes) = self.children.lock() {
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

fn bypass_gateway_proxy(base: &Url) -> bool {
    matches!(base.host(), Some(url::Host::Ipv4(ip)) if ip.is_loopback())
        || matches!(base.host(), Some(url::Host::Ipv6(ip)) if ip.is_loopback())
        || base
            .host_str()
            .is_some_and(|host| host.eq_ignore_ascii_case("localhost"))
}

fn client(base: &Url) -> Result<Client, String> {
    let mut builder = Client::builder();
    if bypass_gateway_proxy(base) {
        builder = builder
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none());
    }
    builder
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
pub async fn authenticate_connection(
    base_url: String,
    credential_ref: Option<String>,
) -> Result<AuthResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        authenticate_connection_blocking(base_url, credential_ref)
    })
    .await
    .map_err(|error| format!("Gateway authentication task failed: {error}"))?
}

fn authenticate_connection_blocking(
    base_url: String,
    credential_ref: Option<String>,
) -> Result<AuthResult, String> {
    let base = parse_base_url(&base_url)?;
    let http = client(&base)?;
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
    let http = client(base)?;
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
    let mut command = Command::new(candidate);
    configure_python_utf8(&mut command);
    command.arg("--version");
    let raw = run_probe_command(&mut command, Duration::from_secs(10)).ok()?;
    let version = raw.split_whitespace().find(|part| {
        part.chars()
            .next()
            .is_some_and(|character| character.is_ascii_digit())
    })?;
    let mut parts = version.split('.');
    Some((parts.next()?.parse().ok()?, parts.next()?.parse().ok()?))
}

fn supported_gateway_python(candidate: &str) -> bool {
    python_version(candidate).is_some_and(|(major, minor)| {
        major > MIN_PYTHON_MAJOR || (major == MIN_PYTHON_MAJOR && minor >= MIN_PYTHON_MINOR)
    })
}

fn push_unique(candidates: &mut Vec<String>, candidate: String) {
    if !candidates.contains(&candidate) {
        candidates.push(candidate);
    }
}

#[cfg(target_os = "macos")]
fn push_path(candidates: &mut Vec<String>, path: &Path) {
    push_unique(candidates, path.to_string_lossy().into_owned());
}

#[cfg(target_os = "macos")]
fn extend_version_managed_pythons(candidates: &mut Vec<String>, root: &Path) {
    let Ok(entries) = std::fs::read_dir(root) else {
        return;
    };
    let mut paths = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path().join("bin/python3"))
        .filter(|path| path.is_file())
        .collect::<Vec<_>>();
    paths.sort();
    paths.reverse();
    for path in paths {
        push_path(candidates, &path);
    }
}

#[cfg(target_os = "macos")]
fn extend_homebrew_pythons(candidates: &mut Vec<String>, root: &Path) {
    let Ok(entries) = std::fs::read_dir(root) else {
        return;
    };
    for entry in entries.filter_map(Result::ok) {
        if !entry.file_name().to_string_lossy().starts_with("python@") {
            continue;
        }
        for relative in ["libexec/bin/python3", "bin/python3"] {
            let path = entry.path().join(relative);
            if path.is_file() {
                push_path(candidates, &path);
            }
        }
    }
}

#[cfg(target_os = "macos")]
fn extend_macos_python_candidates(candidates: &mut Vec<String>, home: Option<&Path>) {
    for path in [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/opt/local/bin/python3",
        "/opt/anaconda3/bin/python3",
        "/opt/miniconda3/bin/python3",
        "/opt/miniforge3/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
    ] {
        push_path(candidates, Path::new(path));
    }
    for root in [Path::new("/opt/homebrew/opt"), Path::new("/usr/local/opt")] {
        extend_homebrew_pythons(candidates, root);
    }
    if let Some(home) = home {
        for relative in [
            "anaconda3/bin/python3",
            "miniconda3/bin/python3",
            "miniforge3/bin/python3",
            "mambaforge/bin/python3",
            ".local/bin/python3",
            ".pyenv/shims/python3",
            ".asdf/shims/python3",
        ] {
            push_path(candidates, &home.join(relative));
        }
        for relative in [
            ".pyenv/versions",
            ".asdf/installs/python",
            ".local/share/uv/python",
        ] {
            extend_version_managed_pythons(candidates, &home.join(relative));
        }
    }
}

fn python_candidates(configured: Option<&str>, include_managed: bool) -> Vec<String> {
    let mut candidates = Vec::new();
    if let Some(value) = configured.filter(|value| !value.trim().is_empty()) {
        push_unique(&mut candidates, value.to_string());
    }
    if include_managed {
        if let Ok(environment) = managed_gateway_environment_dir() {
            push_unique(
                &mut candidates,
                managed_gateway_python_path(&environment)
                    .to_string_lossy()
                    .into_owned(),
            );
        }
    }
    push_unique(&mut candidates, "python3".to_string());
    push_unique(&mut candidates, "python".to_string());
    #[cfg(target_os = "windows")]
    push_unique(&mut candidates, "py".to_string());

    // Finder-launched macOS apps do not inherit the user's shell PATH. Check
    // common package-manager and version-manager locations explicitly so the
    // packaged app sees the same Python installations as a terminal session.
    #[cfg(target_os = "macos")]
    extend_macos_python_candidates(&mut candidates, dirs::home_dir().as_deref());
    candidates
}

#[cfg(debug_assertions)]
fn detect_development_python(configured: Option<&str>) -> Result<String, String> {
    for candidate in python_candidates(configured, false) {
        if supported_gateway_python(&candidate) {
            return Ok(candidate);
        }
    }
    Err("Python 3.10 or newer was not found. Install Python or set a Python path in Desktop settings.".to_string())
}

fn detect_document_engine_python(configured: Option<&str>) -> Result<String, String> {
    for candidate in python_candidates(configured, true) {
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
pub async fn document_engine_status(python_path: Option<String>) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run_document_engine_command(python_path.as_deref(), &["status", "--json"])
    })
    .await
    .map_err(|error| format!("Document engine status task failed: {error}"))?
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
pub async fn remove_document_engine(python_path: Option<String>) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run_document_engine_command(python_path.as_deref(), &["remove", "--yes", "--json"])
    })
    .await
    .map_err(|error| format!("Document engine removal task failed: {error}"))?
}

#[cfg(debug_assertions)]
fn installed_gateway_version(python: &str) -> Option<String> {
    let script = "import crabcode_gateway; print(getattr(crabcode_gateway, '__version__', ''))";
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    command.args(["-c", script]);
    let version = run_probe_command(&mut command, Duration::from_secs(15)).ok()?;
    (!version.is_empty()).then_some(version)
}

// Probe imports in a separate, time-limited process: a broken interpreter or
// dependency must not prevent trying the next environment. A file avoids pipe
// backpressure while we wait; only its bounded tail is read into memory.
fn run_probe_command(command: &mut Command, timeout: Duration) -> Result<String, String> {
    let mut output = tempfile::tempfile().map_err(|error| error.to_string())?;
    let mut child = command
        .stdin(Stdio::null())
        .stdout(output.try_clone().map_err(|error| error.to_string())?)
        .stderr(output.try_clone().map_err(|error| error.to_string())?)
        .spawn()
        .map_err(|error| error.to_string())?;
    let deadline = Instant::now() + timeout;
    let result = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Err(error) => break Err(error.to_string()),
            Ok(None) if Instant::now() >= deadline => {
                break Err(format!(
                    "Python check timed out after {} seconds",
                    timeout.as_secs()
                ));
            }
            Ok(None) => thread::sleep(Duration::from_millis(25)),
        }
    };
    if result.is_err() {
        stop_child_tree(&mut child).map_err(|error| error.to_string())?;
    }
    let status = result?;
    let length = output.metadata().map_err(|error| error.to_string())?.len();
    output
        .seek(SeekFrom::Start(length.saturating_sub(16384)))
        .map_err(|error| error.to_string())?;
    let mut bytes = Vec::new();
    output
        .take(16384)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    let detail = String::from_utf8_lossy(&bytes).trim().to_string();
    if status.success() {
        Ok(detail)
    } else {
        Err(format!("Python check failed ({status}): {detail}"))
    }
}

fn check_gateway_installation(python: &str) -> Result<(), String> {
    let script = r#"
import sys
import crabcode_gateway
from crabcode_gateway.protocol import GATEWAY_MIN_PROTOCOL_VERSION, GATEWAY_MAX_PROTOCOL_VERSION
expected, protocol = sys.argv[1], int(sys.argv[2])
if crabcode_gateway.__version__ != expected:
    raise RuntimeError(f"Gateway version {crabcode_gateway.__version__}; Desktop requires {expected}")
if not GATEWAY_MIN_PROTOCOL_VERSION <= protocol <= GATEWAY_MAX_PROTOCOL_VERSION:
    raise RuntimeError(f"Gateway does not support protocol {protocol}")
# Import the actual CLI entry point and HTTP server/routes, not just the small
# package __init__: an installation without the gateway extra can import that.
from crabcode_cli.__main__ import entry
from crabcode_gateway.server import run_server
"#;
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    if let Some(home) = dirs::home_dir() {
        command.current_dir(home);
    }
    command.args([
        "-c",
        script,
        env!("CARGO_PKG_VERSION"),
        &GATEWAY_PROTOCOL.to_string(),
    ]);
    run_probe_command(&mut command, Duration::from_secs(15)).map(|_| ())
}

struct StartedGateway {
    python: String,
    health: Value,
}

fn start_gateway(
    python: &str,
    base: &Url,
    credential_ref: Option<&str>,
    timeout: Duration,
    processes: &GatewayProcesses,
    connection_id: &str,
    progress: &(impl Fn(&str, &str) + Sync),
) -> Result<StartedGateway, String> {
    progress(
        "starting_gateway",
        &format!("正在验证 Gateway 启动能力 · {python}"),
    );
    let host = base.host_str().unwrap_or("127.0.0.1");
    let port = base.port_or_known_default().unwrap_or(4096).to_string();
    let mut command = Command::new(python);
    configure_gateway_command(&mut command, python, host, &port);
    if let Some(home) = dirs::home_dir() {
        command.current_dir(home);
    }
    // Register even candidate processes immediately so closing Desktop during
    // startup stops them as well as already-ready Gateways.
    let mut children = processes
        .children
        .lock()
        .map_err(|_| "Gateway process registry is unavailable".to_string())?;
    if processes.stopping.load(Ordering::Acquire) {
        return Err("Desktop is shutting down".to_string());
    }
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("Unable to start the local Gateway: {error}"))?;
    // Continue draining stderr after startup, retaining at most 16 KiB. This
    // preserves useful failure details without blocking a verbose server.
    let errors = Arc::new(Mutex::new(VecDeque::new()));
    if let Some(mut stderr) = child.stderr.take() {
        let errors = Arc::clone(&errors);
        thread::spawn(move || {
            let mut bytes = [0; 4096];
            while let Ok(count) = stderr.read(&mut bytes) {
                if count == 0 {
                    break;
                }
                let Ok(mut tail) = errors.lock() else { break };
                tail.extend(&bytes[..count]);
                let excess = tail.len().saturating_sub(16384);
                tail.drain(..excess);
            }
        });
    }
    if let Some(mut previous) = children.insert(connection_id.to_string(), child) {
        let _ = stop_child_tree(&mut previous);
    }
    drop(children);
    progress("waiting_gateway", "正在等待本地 Gateway 就绪");
    let deadline = Instant::now() + timeout;
    let result = (|| {
        loop {
            let process_exited = || -> Result<Option<ExitStatus>, String> {
                processes
                    .children
                    .lock()
                    .map_err(|_| "Gateway process registry is unavailable".to_string())?
                    .get_mut(connection_id)
                    .ok_or_else(|| "Gateway startup was cancelled".to_string())?
                    .try_wait()
                    .map_err(|error| error.to_string())
            };
            if let Some(status) = process_exited()? {
                return Err(format!(
                    "The local Gateway process exited before becoming ready ({status})"
                ));
            }
            if Instant::now() >= deadline {
                return Err(format!(
                    "The local Gateway did not become ready within {} seconds",
                    timeout.as_secs()
                ));
            }
            if let Some(health) = probe_health(base, credential_ref)? {
                // Newly launched installations must match the Desktop release;
                // pre-existing servers keep the protocol-only connection policy.
                if health.get("version").and_then(Value::as_str) != Some(env!("CARGO_PKG_VERSION"))
                {
                    return Err(
                        "The started Gateway did not report the required version".to_string()
                    );
                }
                if process_exited()?.is_some() {
                    return Err("The local Gateway exited during its health check".to_string());
                }
                return Ok(health);
            }
            thread::sleep(Duration::from_millis(100));
        }
    })();
    match result {
        Ok(health) => Ok(StartedGateway {
            python: python.to_string(),
            health,
        }),
        Err(error) => {
            stop_registered_gateway(processes, connection_id)
                .map_err(|cleanup| format!("{error}; unable to stop Gateway: {cleanup}"))?;
            let detail = errors
                .lock()
                .map(|tail| {
                    String::from_utf8_lossy(&tail.iter().copied().collect::<Vec<_>>())
                        .trim()
                        .to_string()
                })
                .unwrap_or_default();
            Err(format!("{error}\n{detail}").trim().to_string())
        }
    }
}

// Kept available in debug tests so the packaged-app selection/fallback path is
// exercised without installing packages into a developer's real environment.
#[cfg(any(not(debug_assertions), test))]
fn start_release_gateway_at(
    mut candidates: Vec<String>,
    environment: &Path,
    base: &Url,
    credential_ref: Option<&str>,
    processes: &GatewayProcesses,
    connection_id: &str,
    progress: &(impl Fn(&str, &str) + Sync),
) -> Result<StartedGateway, String> {
    // User-selected and external environments take priority over our managed one.
    push_unique(
        &mut candidates,
        managed_gateway_python_path(environment)
            .to_string_lossy()
            .into_owned(),
    );
    let mut base_python = None;
    for python in candidates {
        if processes.stopping.load(Ordering::Acquire) {
            return Err("Desktop is shutting down".to_string());
        }
        if !supported_gateway_python(&python) {
            continue;
        }
        base_python.get_or_insert_with(|| python.clone());
        progress(
            "checking_package",
            &format!("正在检查已有 CrabCode 的版本和 Gateway 依赖 · {python}"),
        );
        let result = check_gateway_installation(&python).and_then(|()| {
            ensure_default_browser(&python, progress)?;
            start_gateway(
                &python,
                base,
                credential_ref,
                Duration::from_secs(10),
                processes,
                connection_id,
                progress,
            )
        });
        match result {
            Ok(started) => {
                progress(
                    "environment",
                    &format!("已复用现有 CrabCode 安装，版本、依赖和启动检查通过 · {python}"),
                );
                return Ok(started);
            }
            Err(error) => progress(
                "environment",
                &format!("现有安装不可用，继续检测 · {python}\n{error}"),
            ),
        }
    }
    if processes.stopping.load(Ordering::Acquire) {
        return Err("Desktop is shutting down".to_string());
    }
    let base_python = base_python.ok_or_else(|| "Python 3.10 or newer was not found. Install Python or set a Python path in Desktop settings.".to_string())?;
    progress(
        "creating_environment",
        "未找到可复用的安装，正在准备 CrabCode 独立 Python 环境",
    );
    let python = ensure_managed_gateway_python_at(&base_python, environment, &|line| {
        progress("creating_environment", line)
    })?;
    if check_gateway_installation(&python).is_err() {
        progress(
            "installing",
            "正在独立环境中安装 CrabCode、Gateway、Browser 和 Chromium",
        );
        install_gateway(&python, &|line| progress("installing", line))?;
        check_gateway_installation(&python)?;
    }
    start_gateway(
        &python,
        base,
        credential_ref,
        Duration::from_secs(10),
        processes,
        connection_id,
        progress,
    )
}

fn managed_gateway_environment_dir() -> Result<PathBuf, String> {
    let home =
        dirs::home_dir().ok_or_else(|| "Unable to locate the user home directory".to_string())?;
    Ok(home.join(".crabcode").join("desktop").join("gateway-venv"))
}

fn managed_gateway_python_path(environment: &Path) -> PathBuf {
    #[cfg(target_os = "windows")]
    return environment.join("Scripts").join("python.exe");
    #[cfg(not(target_os = "windows"))]
    return environment.join("bin").join("python");
}

fn ensure_managed_gateway_python_at(
    base_python: &str,
    environment: &Path,
    on_output: &(impl Fn(&str) + Sync),
) -> Result<String, String> {
    let managed_python = managed_gateway_python_path(environment);
    let managed_python_string = managed_python.to_string_lossy().into_owned();
    if supported_gateway_python(&managed_python_string) {
        return Ok(managed_python_string);
    }

    let parent = environment
        .parent()
        .ok_or_else(|| "Invalid managed Gateway environment path".to_string())?;
    std::fs::create_dir_all(parent)
        .map_err(|error| format!("Unable to create the Gateway environment directory: {error}"))?;
    let environment_string = environment.to_string_lossy().into_owned();
    let mut command = Command::new(base_python);
    configure_python_utf8(&mut command);
    command.args(["-m", "venv", "--clear", &environment_string]);
    let (status, detail) = run_streaming_command(&mut command, on_output)?;
    if !status.success() {
        return Err(format!(
            "Unable to create the managed Gateway environment with `{base_python} -m venv`. {detail}"
        ));
    }
    if !supported_gateway_python(&managed_python_string) {
        return Err(
            "The managed Gateway environment did not provide a working Python interpreter"
                .to_string(),
        );
    }
    Ok(managed_python_string)
}

#[derive(Deserialize)]
struct PythonEnvironment {
    version: String,
    executable: String,
    prefix: String,
    kind: String,
}

fn python_environment(python: &str) -> Option<PythonEnvironment> {
    let script = r#"
import json, platform, sys
from pathlib import Path
kind = "Conda 环境" if (Path(sys.prefix) / "conda-meta").is_dir() else "虚拟环境" if sys.prefix != sys.base_prefix else "基础环境"
print(json.dumps({"version": platform.python_version(), "executable": sys.executable, "prefix": sys.prefix, "kind": kind}))
"#;
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    command.args(["-c", script]);
    let output = run_probe_command(&mut command, Duration::from_secs(3)).ok()?;
    serde_json::from_str(output.lines().last()?).ok()
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct GatewayStartupProgress {
    connection_id: String,
    operation_id: String,
    stage: String,
    detail: String,
}

// Drain both pipes concurrently so a verbose installer cannot deadlock. Keep only
// a bounded tail for failures; each line is delivered to the UI as it arrives.
fn run_streaming_command(
    command: &mut Command,
    on_output: &(impl Fn(&str) + Sync),
) -> Result<(ExitStatus, String), String> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("Unable to start setup command: {error}"))?;
    let stdout = child.stdout.take().ok_or("Unable to read setup output")?;
    let stderr = child.stderr.take().ok_or("Unable to read setup errors")?;
    let read_output = |stream: &mut dyn Read| -> Result<String, String> {
        let mut tail = VecDeque::new();
        let mut reader = BufReader::new(stream);
        let mut bytes = Vec::new();
        loop {
            bytes.clear();
            if reader
                .read_until(b'\n', &mut bytes)
                .map_err(|error| error.to_string())?
                == 0
            {
                break;
            }
            let line: String = String::from_utf8_lossy(&bytes)
                .trim()
                .chars()
                .take(4096)
                .collect();
            if line.is_empty() {
                continue;
            }
            on_output(&line);
            tail.push_back(line);
            if tail.len() > 40 {
                tail.pop_front();
            }
        }
        Ok(tail.into_iter().collect::<Vec<_>>().join("\n"))
    };
    thread::scope(|scope| {
        let stdout_reader = scope.spawn(|| read_output(&mut { stdout }));
        let stderr_reader = scope.spawn(|| read_output(&mut { stderr }));
        let status = child
            .wait()
            .map_err(|error| format!("Unable to wait for setup command: {error}"))?;
        let stdout = stdout_reader
            .join()
            .map_err(|_| "Setup output reader stopped unexpectedly")??;
        let stderr = stderr_reader
            .join()
            .map_err(|_| "Setup error reader stopped unexpectedly")??;
        Ok((status, [stdout, stderr].join("\n").trim().to_string()))
    })
}

fn default_gateway_package() -> Result<String, String> {
    gateway_features_package(&["browser".to_string()])
}

fn playwright_module_installed(python: &str) -> bool {
    let script = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('playwright') else 1)";
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    command.args(["-c", script]);
    run_probe_command(&mut command, Duration::from_secs(15)).is_ok()
}

fn playwright_browsers_ready(python: &str) -> bool {
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    command.args(["-c", PLAYWRIGHT_BROWSER_READY_SCRIPT]);
    run_probe_command(&mut command, Duration::from_secs(20)).is_ok()
}

fn install_gateway(python: &str, on_output: &(impl Fn(&str) + Sync)) -> Result<(), String> {
    let package = default_gateway_package()?;
    install_gateway_package(python, &package, on_output)?;
    install_playwright_chromium(python, on_output)?;
    check_playwright_chromium(python)
}

fn ensure_default_browser(
    python: &str,
    progress: &(impl Fn(&str, &str) + Sync),
) -> Result<(), String> {
    let module_installed = playwright_module_installed(python);
    if module_installed && playwright_browsers_ready(python) {
        return Ok(());
    }
    progress(
        "installing",
        "正在安装 Browser 与 Chromium，首次补齐可能需要几分钟",
    );
    if module_installed {
        install_playwright_chromium(python, &|line| progress("installing", line))?;
        return check_playwright_chromium(python);
    }
    install_gateway(python, &|line| progress("installing", line))
}

fn install_gateway_package(
    python: &str,
    package: &str,
    on_output: &(impl Fn(&str) + Sync),
) -> Result<(), String> {
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    command.args([
        "-u",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-input",
        "--progress-bar",
        "off",
        &package,
    ]);
    let (status, detail) = run_streaming_command(&mut command, on_output)?;
    if status.success() {
        return Ok(());
    }
    Err(format!(
        "Failed to install {package}. Run `{python} -m pip install --upgrade \"{package}\"` manually. {detail}"
    ))
}

const GATEWAY_INSTALL_FEATURES: &[&str] = &["search", "debugger", "browser"];
const RIPGREP_VERSION: &str = "15.2.0";

#[derive(Clone, Copy)]
struct RipgrepRelease {
    asset: &'static str,
    sha256: &'static str,
}

fn ripgrep_release_for(os: &str, arch: &str) -> Result<RipgrepRelease, String> {
    let release = match (os, arch) {
        ("macos", "aarch64") => RipgrepRelease {
            asset: "ripgrep-15.2.0-aarch64-apple-darwin.tar.gz",
            sha256: "3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4",
        },
        ("macos", "x86_64") => RipgrepRelease {
            asset: "ripgrep-15.2.0-x86_64-apple-darwin.tar.gz",
            sha256: "af7825fcc69a2afc7a7aea55fc9af90e26421d8f20fe59df32e233c0b8a231c1",
        },
        ("windows", "aarch64") => RipgrepRelease {
            asset: "ripgrep-15.2.0-aarch64-pc-windows-msvc.zip",
            sha256: "e4abca10c3a64ebea742667dd7009449d49403db5460dd6873e389fa2945360f",
        },
        ("windows", "x86_64") => RipgrepRelease {
            asset: "ripgrep-15.2.0-x86_64-pc-windows-msvc.zip",
            sha256: "71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5",
        },
        ("windows", "x86") => RipgrepRelease {
            asset: "ripgrep-15.2.0-i686-pc-windows-msvc.zip",
            sha256: "9bf73bdb3fda9ad4b0235e1295b02c717031c986afa4d7c05dd0af8b74010a95",
        },
        ("linux", "aarch64") => RipgrepRelease {
            asset: "ripgrep-15.2.0-aarch64-unknown-linux-musl.tar.gz",
            sha256: "800b1e7206afe799dfb5a6901f23147cfaabe0e52210538100f61e86e1740915",
        },
        ("linux", "x86_64") => RipgrepRelease {
            asset: "ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz",
            sha256: "33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c",
        },
        ("linux", "arm") => RipgrepRelease {
            asset: "ripgrep-15.2.0-armv7-unknown-linux-gnueabihf.tar.gz",
            sha256: "d859589734d9d802107ad9eff6a78cfd9b0080d2fecb0ad8772605b35e373199",
        },
        _ => {
            return Err(format!(
                "ripgrep {RIPGREP_VERSION} does not provide a Desktop installer for {os}/{arch}"
            ))
        }
    };
    Ok(release)
}

fn legacy_gateway_suite_features(suite: &str) -> Result<Vec<String>, String> {
    match suite {
        "gateway" => Ok(Vec::new()),
        "search" => Ok(vec!["search".to_string()]),
        "debugger" => Ok(vec!["debugger".to_string()]),
        "search-debugger" => Ok(vec!["search".to_string(), "debugger".to_string()]),
        _ => Err("Unknown CrabCode suite".to_string()),
    }
}

fn normalize_gateway_install_features(features: Vec<String>) -> Result<Vec<String>, String> {
    for feature in &features {
        if !GATEWAY_INSTALL_FEATURES.contains(&feature.as_str()) {
            return Err(format!("Unknown CrabCode feature: {feature}"));
        }
    }
    Ok(GATEWAY_INSTALL_FEATURES
        .iter()
        .filter(|feature| {
            features
                .iter()
                .any(|selected| selected.as_str() == **feature)
        })
        .map(|feature| (*feature).to_string())
        .collect())
}

fn gateway_install_features(
    features: Option<Vec<String>>,
    legacy_suite: Option<&str>,
) -> Result<Vec<String>, String> {
    if let Some(features) = features {
        return normalize_gateway_install_features(features);
    }
    legacy_gateway_suite_features(legacy_suite.unwrap_or("gateway"))
}

fn gateway_features_package(features: &[String]) -> Result<String, String> {
    let features = normalize_gateway_install_features(features.to_vec())?;
    let mut extras = vec!["gateway".to_string()];
    extras.extend(features);
    Ok(format!(
        "crabcode[{}]=={}",
        extras.join(","),
        env!("CARGO_PKG_VERSION")
    ))
}

#[cfg(test)]
fn gateway_suite_package(suite: &str) -> Result<String, String> {
    gateway_features_package(&legacy_gateway_suite_features(suite)?)
}

fn gateway_feature_modules(features: &[String]) -> Result<Vec<&'static str>, String> {
    let features = normalize_gateway_install_features(features.to_vec())?;
    let mut modules = Vec::new();
    if features.iter().any(|feature| feature == "search") {
        modules.extend([
            "crabcode_search",
            "usearch",
            "tree_sitter_language_pack",
            "sentence_transformers",
            "modelscope",
        ]);
    }
    if features.iter().any(|feature| feature == "debugger") {
        modules.push("crabcode_debugger");
    }
    if features.iter().any(|feature| feature == "browser") {
        modules.push("playwright");
    }
    Ok(modules)
}

fn features_from_probe_output(output: &str) -> Result<Vec<String>, String> {
    let parsed: Vec<String> = serde_json::from_str(output.trim().lines().last().unwrap_or("[]"))
        .map_err(|error| error.to_string())?;
    let known = parsed
        .into_iter()
        .filter(|feature| GATEWAY_INSTALL_FEATURES.contains(&feature.as_str()))
        .collect();
    normalize_gateway_install_features(known)
}

fn python_for_feature_probe(configured: Option<&str>) -> Result<String, String> {
    let mut candidates = python_candidates(configured, false);
    if let Ok(environment) = managed_gateway_environment_dir() {
        push_unique(
            &mut candidates,
            managed_gateway_python_path(&environment)
                .to_string_lossy()
                .into_owned(),
        );
    }
    let mut fallback = None;
    for candidate in candidates {
        if !supported_gateway_python(&candidate) {
            continue;
        }
        if fallback.is_none() {
            fallback = Some(candidate.clone());
        }
        if check_gateway_installation(&candidate).is_ok() {
            return Ok(candidate);
        }
    }
    fallback.ok_or_else(|| {
        "Python 3.10 or newer was not found. Install Python or set a Python path in Desktop settings."
            .to_string()
    })
}

fn probe_installed_gateway_features(python: &str) -> Result<Vec<String>, String> {
    let script = r#"
import importlib.util, json
features = []
if importlib.util.find_spec("crabcode_search"):
    features.append("search")
if importlib.util.find_spec("crabcode_debugger"):
    features.append("debugger")
if importlib.util.find_spec("playwright"):
    import os, sys
    from pathlib import Path
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override and override != "0":
        browsers = Path(override)
    elif sys.platform == "win32":
        browsers = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    elif sys.platform == "darwin":
        browsers = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        browsers = Path.home() / ".cache" / "ms-playwright"
    import playwright
    catalog = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    revision = next(item["revision"] for item in json.loads(catalog.read_text(encoding="utf-8"))["browsers"] if item["name"] == "chromium-headless-shell")
    directory = browsers / f"chromium_headless_shell-{revision}"
    if any(path.is_file() for path in directory.rglob("chrome-headless-shell*")):
        features.append("browser")
print(json.dumps(features))
"#;
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    if let Some(home) = dirs::home_dir() {
        command.current_dir(home);
    }
    command.args(["-c", script]);
    let output = run_probe_command(&mut command, Duration::from_secs(15))?;
    features_from_probe_output(&output)
}

fn gateway_suite_name(features: &[String]) -> String {
    if features.is_empty() {
        "gateway".to_string()
    } else {
        features.join("-")
    }
}

fn ripgrep_version(python: &str) -> Result<String, String> {
    let binary_name = if cfg!(target_os = "windows") {
        "rg.exe"
    } else {
        "rg"
    };
    let mut candidates = Vec::new();
    if let Some(directory) = python_scripts_directory(python) {
        candidates.push(directory.join(binary_name));
    }
    candidates.push(PathBuf::from(binary_name));
    let search_path = gateway_search_path(python);
    for candidate in candidates {
        let mut command = Command::new(&candidate);
        configure_python_utf8(&mut command);
        if let Some(path) = &search_path {
            command.env("PATH", path);
        }
        command.arg("--version");
        if let Ok(output) = run_probe_command(&mut command, Duration::from_secs(10)) {
            if let Some(version) = output
                .lines()
                .next()
                .filter(|line| line.starts_with("ripgrep "))
            {
                return Ok(version.to_string());
            }
        }
    }
    Err("ripgrep (rg) was not found in the Gateway environment".to_string())
}

fn install_ripgrep(python: &str, on_output: &(impl Fn(&str) + Sync)) -> Result<String, String> {
    if let Ok(version) = ripgrep_version(python) {
        on_output(&format!("已检测到 {version}，直接复用"));
        return Ok(version);
    }
    on_output("未检测到 ripgrep，正在安装 rg");
    let release = ripgrep_release_for(std::env::consts::OS, std::env::consts::ARCH)?;
    let scripts = python_scripts_directory(python)
        .ok_or_else(|| "Unable to locate the selected Python scripts directory".to_string())?;
    std::fs::create_dir_all(&scripts)
        .map_err(|error| format!("Unable to create {}: {error}", scripts.display()))?;
    let binary = scripts.join(if cfg!(target_os = "windows") {
        "rg.exe"
    } else {
        "rg"
    });
    let url = format!(
        "https://github.com/BurntSushi/ripgrep/releases/download/{RIPGREP_VERSION}/{}",
        release.asset
    );
    let binary = binary.to_string_lossy().into_owned();
    let script = r#"
import hashlib, io, os, pathlib, stat, sys, tarfile, urllib.request, zipfile

url, expected, output_raw = sys.argv[1:]
output = pathlib.Path(output_raw)
request = urllib.request.Request(url, headers={"User-Agent": "CrabCode-Desktop/ripgrep-installer"})
with urllib.request.urlopen(request, timeout=120) as response:
    chunks, size = [], 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > 32 * 1024 * 1024:
            raise RuntimeError("ripgrep archive exceeds the 32 MiB safety limit")
        chunks.append(chunk)
payload = b"".join(chunks)
actual = hashlib.sha256(payload).hexdigest()
if actual != expected:
    raise RuntimeError(f"ripgrep archive checksum mismatch: expected {expected}, got {actual}")
binary_name = "rg.exe" if os.name == "nt" else "rg"
if url.endswith(".zip"):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [name for name in archive.namelist() if pathlib.PurePosixPath(name).name == binary_name]
        if len(members) != 1:
            raise RuntimeError(f"expected one {binary_name} in the ripgrep archive, found {len(members)}")
        binary = archive.read(members[0])
else:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if pathlib.PurePosixPath(member.name).name == binary_name and member.isfile()]
        if len(members) != 1:
            raise RuntimeError(f"expected one {binary_name} in the ripgrep archive, found {len(members)}")
        source = archive.extractfile(members[0])
        if source is None:
            raise RuntimeError(f"unable to read {binary_name} from the ripgrep archive")
        binary = source.read()
temporary = output.with_name(output.name + ".tmp")
temporary.write_bytes(binary)
if os.name != "nt":
    temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
temporary.replace(output)
print(f"installed {binary_name} to {output}")
"#;
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    command.args(["-u", "-c", script, &url, release.sha256, &binary]);
    let (status, detail) = run_streaming_command(&mut command, on_output)?;
    if !status.success() {
        return Err(format!(
            "Failed to install ripgrep {RIPGREP_VERSION} from the official release archive. {detail}"
        ));
    }
    let version = ripgrep_version(python)?;
    on_output(&format!("已安装 {version}"));
    Ok(version)
}

const PLAYWRIGHT_BROWSER_READY_SCRIPT: &str = r#"
import json, os, sys
from pathlib import Path
# crabcode-playwright-browser-ready
import playwright
override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
if override and override != "0":
    browsers = Path(override)
elif sys.platform == "win32":
    browsers = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
elif sys.platform == "darwin":
    browsers = Path.home() / "Library" / "Caches" / "ms-playwright"
else:
    browsers = Path.home() / ".cache" / "ms-playwright"
catalog = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
revision = next(item["revision"] for item in json.loads(catalog.read_text(encoding="utf-8"))["browsers"] if item["name"] == "chromium-headless-shell")
directory = browsers / f"chromium_headless_shell-{revision}"
shells = [path for path in directory.rglob("chrome-headless-shell*") if path.is_file()]
if not shells:
    raise SystemExit(f"Chromium headless shell is not installed at {directory}")
print(shells[0])
"#;

fn install_playwright_chromium(
    python: &str,
    on_output: &(impl Fn(&str) + Sync),
) -> Result<(), String> {
    on_output("正在下载 Playwright Chromium，首次安装可能需要几分钟");
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    command.args(["-u", "-m", "playwright", "install", "chromium"]);
    let (status, detail) = run_streaming_command(&mut command, on_output)?;
    if status.success() {
        return Ok(());
    }
    Err(format!(
        "Failed to install Playwright Chromium. Run `{python} -m playwright install chromium` manually. {detail}"
    ))
}

fn check_playwright_chromium(python: &str) -> Result<(), String> {
    let mut command = Command::new(python);
    configure_python_utf8(&mut command);
    if let Some(home) = dirs::home_dir() {
        command.current_dir(home);
    }
    command.args(["-c", PLAYWRIGHT_BROWSER_READY_SCRIPT]);
    run_probe_command(&mut command, Duration::from_secs(20)).map_err(|error| {
        format!(
            "Playwright is installed, but the Chromium headless shell is not ready. Run `{python} -m playwright install chromium`. {error}"
        )
    })?;
    Ok(())
}

fn check_gateway_feature_installation(python: &str, features: &[String]) -> Result<(), String> {
    check_gateway_installation(python)?;
    let modules = gateway_feature_modules(features)?;
    if !modules.is_empty() {
        let encoded = serde_json::to_string(&modules).map_err(|error| error.to_string())?;
        let script = r#"
import importlib.util, json, sys
modules = json.loads(sys.argv[1])
missing = [name for name in modules if importlib.util.find_spec(name) is None]
if missing:
    raise RuntimeError("Missing suite modules: " + ", ".join(missing))
"#;
        let mut command = Command::new(python);
        configure_python_utf8(&mut command);
        if let Some(home) = dirs::home_dir() {
            command.current_dir(home);
        }
        command.args(["-c", script, &encoded]);
        run_probe_command(&mut command, Duration::from_secs(15))?;
    }
    if features.iter().any(|feature| feature == "browser") {
        check_playwright_chromium(python)?;
    }
    Ok(())
}

fn resolve_gateway_install_python(configured: Option<&str>) -> Result<String, String> {
    let base_candidates = python_candidates(configured, false);
    let mut installed_candidates = base_candidates.clone();
    if let Ok(environment) = managed_gateway_environment_dir() {
        push_unique(
            &mut installed_candidates,
            managed_gateway_python_path(&environment)
                .to_string_lossy()
                .into_owned(),
        );
    }
    for candidate in installed_candidates {
        if supported_gateway_python(&candidate) && check_gateway_installation(&candidate).is_ok() {
            return Ok(candidate);
        }
    }

    let base_python = base_candidates
        .into_iter()
        .find(|candidate| supported_gateway_python(candidate))
        .ok_or_else(|| {
            "Python 3.10 or newer was not found. Install Python or set a Python path in Desktop settings."
                .to_string()
        })?;
    let environment = managed_gateway_environment_dir()?;
    ensure_managed_gateway_python_at(&base_python, &environment, &|_| {})
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct GatewaySuiteInstallProgress {
    operation_id: String,
    stage: String,
    detail: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GatewaySuiteInstallResult {
    suite: String,
    features: Vec<String>,
    package_spec: String,
    python: String,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct SystemToolInstallProgress {
    operation_id: String,
    stage: String,
    detail: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SystemToolInstallResult {
    tool: String,
    version: String,
    python: String,
}

fn emit_gateway_suite_progress(app: &AppHandle, operation_id: &str, stage: &str, detail: &str) {
    let _ = app.emit(
        "gateway-suite-install-progress",
        GatewaySuiteInstallProgress {
            operation_id: operation_id.to_string(),
            stage: stage.to_string(),
            detail: detail.to_string(),
        },
    );
}

fn emit_system_tool_progress(app: &AppHandle, operation_id: &str, stage: &str, detail: &str) {
    let _ = app.emit(
        "system-tool-install-progress",
        SystemToolInstallProgress {
            operation_id: operation_id.to_string(),
            stage: stage.to_string(),
            detail: detail.to_string(),
        },
    );
}

#[tauri::command]
pub async fn installed_gateway_features(
    python_path: Option<String>,
) -> Result<Vec<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let Ok(python) = python_for_feature_probe(python_path.as_deref()) else {
            return Ok(Vec::new());
        };
        probe_installed_gateway_features(&python).or_else(|_| Ok(Vec::new()))
    })
    .await
    .map_err(|error| format!("Gateway feature probe failed: {error}"))?
}

#[tauri::command]
pub async fn install_gateway_suite(
    app: AppHandle,
    python_path: Option<String>,
    features: Option<Vec<String>>,
    suite: Option<String>,
    operation_id: String,
) -> Result<GatewaySuiteInstallResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let features = gateway_install_features(features, suite.as_deref())?;
        let package_spec = gateway_features_package(&features)?;
        let processes = app.state::<GatewayProcesses>();
        let _startup = processes
            .startup
            .lock()
            .map_err(|_| "Gateway setup registry is unavailable".to_string())?;
        emit_gateway_suite_progress(
            &app,
            &operation_id,
            "selecting_environment",
            "正在定位 Desktop 使用的本地 Python 环境",
        );
        let python = resolve_gateway_install_python(python_path.as_deref())?;
        emit_gateway_suite_progress(
            &app,
            &operation_id,
            "installing",
            &format!("正在安装 {package_spec} · {python}"),
        );
        install_gateway_package(&python, &package_spec, &|line| {
            emit_gateway_suite_progress(&app, &operation_id, "installing", line)
        })?;
        if features.iter().any(|feature| feature == "browser") {
            emit_gateway_suite_progress(
                &app,
                &operation_id,
                "installing_browser",
                "正在安装 Browser 使用的 Chromium",
            );
            install_playwright_chromium(&python, &|line| {
                emit_gateway_suite_progress(&app, &operation_id, "installing_browser", line)
            })?;
        }
        emit_gateway_suite_progress(
            &app,
            &operation_id,
            "verifying",
            "正在验证套件模块与 Gateway 版本",
        );
        check_gateway_feature_installation(&python, &features)?;
        emit_gateway_suite_progress(
            &app,
            &operation_id,
            "complete",
            &format!("{package_spec} 安装完成"),
        );
        Ok(GatewaySuiteInstallResult {
            suite: gateway_suite_name(&features),
            features,
            package_spec,
            python,
        })
    })
    .await
    .map_err(|error| format!("Gateway suite installer task failed: {error}"))?
}

#[tauri::command]
pub async fn install_system_tool(
    app: AppHandle,
    python_path: Option<String>,
    tool: String,
    operation_id: String,
) -> Result<SystemToolInstallResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if tool != "ripgrep" {
            return Err(format!("Unknown system tool: {tool}"));
        }
        let processes = app.state::<GatewayProcesses>();
        let _startup = processes
            .startup
            .lock()
            .map_err(|_| "Gateway setup registry is unavailable".to_string())?;
        emit_system_tool_progress(
            &app,
            &operation_id,
            "selecting_environment",
            "正在定位 Desktop 使用的本地 Python 环境",
        );
        let python = resolve_gateway_install_python(python_path.as_deref())?;
        emit_system_tool_progress(&app, &operation_id, "detecting", "正在检测 ripgrep (rg)");
        let version = install_ripgrep(&python, &|line| {
            emit_system_tool_progress(&app, &operation_id, "installing", line)
        })?;
        emit_system_tool_progress(
            &app,
            &operation_id,
            "complete",
            &format!("{version} 已可用"),
        );
        Ok(SystemToolInstallResult {
            tool,
            version,
            python,
        })
    })
    .await
    .map_err(|error| format!("System tool installer task failed: {error}"))?
}

#[tauri::command]
pub async fn ensure_local_gateway(
    app: AppHandle,
    connection_id: String,
    base_url: String,
    python_path: Option<String>,
    credential_ref: Option<String>,
    operation_id: String,
) -> Result<EnsureGatewayResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let progress = |stage: &str, detail: &str| {
            let _ = app.emit(
                "gateway-startup-progress",
                GatewayStartupProgress {
                    connection_id: connection_id.clone(),
                    operation_id: operation_id.clone(),
                    stage: stage.to_string(),
                    detail: detail.to_string(),
                },
            );
        };
        let result = ensure_local_gateway_blocking(
            app.state::<GatewayProcesses>(),
            connection_id.clone(),
            base_url,
            python_path,
            credential_ref,
            &progress,
        );
        match &result {
            Ok(_) => progress("ready", "本地 Gateway 已就绪"),
            Err(error) => progress("error", error),
        }
        result
    })
    .await
    .map_err(|error| format!("Gateway startup task failed: {error}"))?
}

fn ensure_local_gateway_blocking(
    processes: tauri::State<'_, GatewayProcesses>,
    connection_id: String,
    base_url: String,
    python_path: Option<String>,
    credential_ref: Option<String>,
    progress: &(impl Fn(&str, &str) + Sync),
) -> Result<EnsureGatewayResult, String> {
    let base = parse_base_url(&base_url)?;
    if !is_loopback(&base) {
        return Err(
            "Desktop may only install and start Gateway processes for loopback addresses"
                .to_string(),
        );
    }
    // Several saved local connections can start together. Serialize provisioning
    // so they cannot run pip or spawn a process for the same environment twice.
    let _startup = match processes.startup.try_lock() {
        Ok(guard) => guard,
        Err(std::sync::TryLockError::WouldBlock) => {
            progress("waiting_setup", "正在等待其他本地环境初始化完成");
            processes
                .startup
                .lock()
                .map_err(|_| "Gateway startup registry is unavailable")?
        }
        Err(std::sync::TryLockError::Poisoned(_)) => {
            return Err("Gateway startup registry is unavailable".to_string());
        }
    };
    progress("checking_gateway", "正在检查本地 Gateway");
    if let Some(health) = probe_health(&base, credential_ref.as_deref())? {
        return Ok(EnsureGatewayResult {
            ready: true,
            started_by_desktop: processes
                .children
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

    progress("checking_python", "正在检测 Python 环境");
    #[cfg(debug_assertions)]
    let started = {
        let python = detect_development_python(python_path.as_deref())?;
        progress(
            "environment",
            &format!("开发模式直接使用 Python 环境：{python}"),
        );
        let installed_version = installed_gateway_version(&python);
        if installed_version.as_deref() != Some(env!("CARGO_PKG_VERSION")) {
            progress(
                "installing",
                "正在安装 CrabCode、Browser 和 Chromium，首次启动可能需要几分钟",
            );
            install_gateway(&python, &|line| progress("installing", line))?;
        } else {
            ensure_default_browser(&python, progress)?;
        }
        start_gateway(
            &python,
            &base,
            credential_ref.as_deref(),
            Duration::from_secs(10),
            &processes,
            &connection_id,
            progress,
        )?
    };
    #[cfg(not(debug_assertions))]
    let started = start_release_gateway_at(
        python_candidates(python_path.as_deref(), false),
        &managed_gateway_environment_dir()?,
        &base,
        credential_ref.as_deref(),
        &processes,
        &connection_id,
        progress,
    )?;
    let python = &started.python;
    if let Some(environment) = python_environment(python) {
        progress(
            "environment",
            &format!(
                "本地启动环境：Python {} · {}",
                environment.version, environment.kind
            ),
        );
        progress(
            "environment",
            &format!("Python 解释器路径：{}", environment.executable),
        );
        progress(
            "environment",
            &format!("Python 环境目录：{}", environment.prefix),
        );
    } else {
        progress("environment", "无法读取 Python 环境详情，Gateway 已启动");
    }
    let StartedGateway { python, health } = started;
    Ok(EnsureGatewayResult {
        ready: true,
        started_by_desktop: true,
        python: Some(python),
        version: health
            .get("version")
            .and_then(Value::as_str)
            .map(str::to_string),
        message: "Desktop started the local Gateway".to_string(),
    })
}

#[tauri::command]
pub fn shutdown_gateway(
    processes: tauri::State<'_, GatewayProcesses>,
    connection_id: String,
) -> Result<bool, String> {
    stop_registered_gateway(&processes, &connection_id)
}

fn stop_registered_gateway(
    processes: &GatewayProcesses,
    connection_id: &str,
) -> Result<bool, String> {
    let child = processes
        .children
        .lock()
        .map_err(|_| "Gateway process registry is unavailable".to_string())?
        .remove(connection_id);
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
    fn gateway_suite_specs_keep_gateway_and_desktop_version() {
        assert_eq!(
            gateway_suite_package("gateway").unwrap(),
            format!("crabcode[gateway]=={}", env!("CARGO_PKG_VERSION"))
        );
        assert_eq!(
            default_gateway_package().unwrap(),
            format!("crabcode[gateway,browser]=={}", env!("CARGO_PKG_VERSION"))
        );
        assert_eq!(
            gateway_suite_package("search").unwrap(),
            format!("crabcode[gateway,search]=={}", env!("CARGO_PKG_VERSION"))
        );
        assert_eq!(
            gateway_suite_package("debugger").unwrap(),
            format!("crabcode[gateway,debugger]=={}", env!("CARGO_PKG_VERSION"))
        );
        assert_eq!(
            gateway_suite_package("search-debugger").unwrap(),
            format!(
                "crabcode[gateway,search,debugger]=={}",
                env!("CARGO_PKG_VERSION")
            )
        );
        assert!(gateway_suite_package("unknown").is_err());
    }

    #[test]
    fn gateway_features_are_independent_canonical_and_backward_compatible() {
        let selected = gateway_install_features(
            Some(vec![
                "debugger".to_string(),
                "search".to_string(),
                "debugger".to_string(),
            ]),
            None,
        )
        .unwrap();
        assert_eq!(selected, vec!["search".to_string(), "debugger".to_string()]);
        assert_eq!(
            gateway_features_package(&selected).unwrap(),
            format!(
                "crabcode[gateway,search,debugger]=={}",
                env!("CARGO_PKG_VERSION")
            )
        );
        let with_browser = gateway_install_features(
            Some(vec!["browser".to_string(), "search".to_string()]),
            None,
        )
        .unwrap();
        assert_eq!(
            with_browser,
            vec!["search".to_string(), "browser".to_string()]
        );
        assert_eq!(
            gateway_features_package(&with_browser).unwrap(),
            format!(
                "crabcode[gateway,search,browser]=={}",
                env!("CARGO_PKG_VERSION")
            )
        );
        assert_eq!(
            gateway_feature_modules(&vec!["browser".to_string()]).unwrap(),
            vec!["playwright"]
        );
        assert_eq!(
            features_from_probe_output("[\"browser\", \"search\", \"nope\"]").unwrap(),
            vec!["search".to_string(), "browser".to_string()]
        );
        assert_eq!(
            gateway_install_features(None, Some("search-debugger")).unwrap(),
            selected
        );
        assert!(gateway_install_features(Some(vec!["ripgrep".to_string()]), None).is_err());
        assert!(gateway_install_features(Some(vec!["unknown".to_string()]), None).is_err());
    }

    #[test]
    fn ripgrep_release_assets_are_pinned_for_desktop_targets() {
        for (os, arch, suffix) in [
            ("macos", "aarch64", "aarch64-apple-darwin.tar.gz"),
            ("macos", "x86_64", "x86_64-apple-darwin.tar.gz"),
            ("windows", "aarch64", "aarch64-pc-windows-msvc.zip"),
            ("windows", "x86_64", "x86_64-pc-windows-msvc.zip"),
            ("windows", "x86", "i686-pc-windows-msvc.zip"),
            ("linux", "aarch64", "aarch64-unknown-linux-musl.tar.gz"),
            ("linux", "x86_64", "x86_64-unknown-linux-musl.tar.gz"),
            ("linux", "arm", "armv7-unknown-linux-gnueabihf.tar.gz"),
        ] {
            let release = ripgrep_release_for(os, arch).unwrap();
            assert!(release.asset.ends_with(suffix));
            assert_eq!(release.sha256.len(), 64);
            assert!(release.sha256.bytes().all(|byte| byte.is_ascii_hexdigit()));
        }
        assert!(ripgrep_release_for("linux", "riscv64").is_err());
    }

    #[cfg(unix)]
    #[test]
    fn ripgrep_detection_reuses_the_python_scripts_binary() {
        use std::os::unix::fs::PermissionsExt;

        let directory = tempfile::tempdir().unwrap();
        let scripts = directory.path().join("scripts");
        std::fs::create_dir_all(&scripts).unwrap();
        let rg = scripts.join("rg");
        std::fs::write(&rg, "#!/bin/sh\nprintf 'ripgrep 14.1.0 (test)\\n'\n").unwrap();
        std::fs::set_permissions(&rg, std::fs::Permissions::from_mode(0o755)).unwrap();

        let python = directory.path().join("python");
        std::fs::write(
            &python,
            format!(
                "#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then printf '%s\\n' '{}'; exit 0; fi\nexit 99\n",
                scripts.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o755)).unwrap();

        let mut gateway = Command::new(&python);
        configure_gateway_command(&mut gateway, &python.to_string_lossy(), "127.0.0.1", "4096");
        let path = gateway
            .get_envs()
            .find_map(|(key, value)| (key == "PATH").then(|| value.unwrap().to_owned()))
            .unwrap();
        assert!(std::env::split_paths(&path).any(|entry| entry == scripts));

        let output = Mutex::new(Vec::new());
        install_ripgrep(&python.to_string_lossy(), &|line| {
            output.lock().unwrap().push(line.to_string())
        })
        .unwrap();
        assert_eq!(
            ripgrep_version(&python.to_string_lossy()).unwrap(),
            "ripgrep 14.1.0 (test)"
        );
        assert_eq!(
            *output.lock().unwrap(),
            ["已检测到 ripgrep 14.1.0 (test)，直接复用"]
        );
    }

    #[cfg(unix)]
    #[test]
    fn streams_installer_output_before_exit_and_keeps_failure_details() {
        let directory = tempfile::tempdir().unwrap();
        let marker = directory.path().join("progress-received");
        let mut command = Command::new("/bin/sh");
        // The child can only continue after its first line reaches the callback.
        command
            .args([
                "-c",
                r#"
            printf 'installing\n'
            attempt=0
            while [ ! -f "$1" ]; do
                attempt=$((attempt + 1))
                [ "$attempt" -lt 100 ] || exit 99
                sleep 0.01
            done
            printf 'download failed\n' >&2
            exit 7
        "#,
                "installer-test",
            ])
            .arg(&marker);
        let output = Mutex::new(Vec::new());
        let (status, tail) = run_streaming_command(&mut command, &|line| {
            output.lock().unwrap().push(line.to_string());
            if line == "installing" {
                std::fs::write(&marker, "received").unwrap();
            }
        })
        .unwrap();
        assert_eq!(status.code(), Some(7));
        assert_eq!(*output.lock().unwrap(), ["installing", "download failed"]);
        assert!(tail.contains("download failed"));
    }

    #[cfg(unix)]
    #[test]
    fn bounds_installer_history_without_dropping_live_output() {
        let mut command = Command::new("/bin/sh");
        command.args([
            "-c",
            "i=0; while [ $i -lt 100 ]; do echo line-$i; echo error-$i >&2; i=$((i + 1)); done",
        ]);
        let output = Mutex::new(Vec::new());
        let (status, tail) = run_streaming_command(&mut command, &|line| {
            output.lock().unwrap().push(line.to_string());
        })
        .unwrap();
        assert!(status.success());
        assert_eq!(output.lock().unwrap().len(), 200);
        assert_eq!(tail.lines().count(), 80);
        assert!(tail.contains("line-99"));
        assert!(tail.contains("error-99"));
        assert!(!tail.contains("line-0\n"));
    }

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
    fn proxy_bypass_is_limited_to_literal_loopback() {
        for url in [
            "http://localhost:4096",
            "http://127.0.0.1:4096",
            "http://[::1]:4096",
        ] {
            assert!(bypass_gateway_proxy(&parse_base_url(url).unwrap()));
        }
        for url in [
            "https://example.com",
            "http://192.168.1.2:4096",
            "http://localhost.example.com",
        ] {
            assert!(!bypass_gateway_proxy(&parse_base_url(url).unwrap()));
        }
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
    fn gateway_launch_allows_the_macos_tauri_origin() {
        let mut command = Command::new("python");
        configure_gateway_command(&mut command, "python", "127.0.0.1", "4096");
        let arguments = command
            .get_args()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert!(arguments
            .windows(2)
            .any(|pair| pair == ["--cors", DESKTOP_ORIGIN]));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn finds_conda_python_without_a_shell_path() {
        use std::os::unix::fs::PermissionsExt;

        let directory = tempfile::tempdir().unwrap();
        let python = directory.path().join("anaconda3/bin/python3");
        std::fs::create_dir_all(python.parent().unwrap()).unwrap();
        std::fs::write(&python, "#!/bin/sh\nprintf 'Python 3.12.4\\n'\n").unwrap();
        std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o755)).unwrap();

        let mut candidates = Vec::new();
        extend_macos_python_candidates(&mut candidates, Some(directory.path()));
        let discovered = python.to_string_lossy().into_owned();
        assert!(candidates.contains(&discovered));
        assert_eq!(python_version(&discovered), Some((3, 12)));
    }

    #[cfg(unix)]
    #[test]
    fn creates_and_reuses_an_isolated_gateway_environment() {
        use std::os::unix::fs::PermissionsExt;

        let directory = tempfile::tempdir().unwrap();
        let base_python = directory.path().join("base-python");
        std::fs::write(
            &base_python,
            r#"#!/bin/sh
if [ "$1" = "--version" ]; then
  printf 'Python 3.12.4\n'
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ] && [ "$3" = "--clear" ]; then
  mkdir -p "$4/bin"
  cp "$0" "$4/bin/python"
  printf 'created isolated environment\n'
  exit 0
fi
exit 2
"#,
        )
        .unwrap();
        std::fs::set_permissions(&base_python, std::fs::Permissions::from_mode(0o755)).unwrap();
        let environment = directory.path().join("managed/gateway-venv");
        let output = Mutex::new(Vec::new());
        let capture = |line: &str| output.lock().unwrap().push(line.to_string());

        let first = ensure_managed_gateway_python_at(
            &base_python.to_string_lossy(),
            &environment,
            &capture,
        )
        .unwrap();
        let second = ensure_managed_gateway_python_at(
            &base_python.to_string_lossy(),
            &environment,
            &capture,
        )
        .unwrap();

        assert_eq!(first, second);
        assert_eq!(Path::new(&first), managed_gateway_python_path(&environment));
        assert_eq!(*output.lock().unwrap(), ["created isolated environment"]);
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

#[cfg(all(test, unix))]
#[path = "gateway_runtime_tests.rs"]
mod runtime_tests;
