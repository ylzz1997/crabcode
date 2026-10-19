//! Authenticated, per-user native executor. All transports share one input queue.
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[cfg(target_os = "macos")]
mod permission_guide {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;

    unsafe extern "C" {
        fn crab_show_permission_drag_guide(
            application_path: *const std::ffi::c_char,
            screen_recording: bool,
        ) -> bool;
        fn crab_hide_permission_drag_guide_if_granted(
            capture_granted: bool,
            accessibility_granted: bool,
        );
    }

    pub fn installed_bundle(executable: &Path) -> Result<&Path, String> {
        executable
            .parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .filter(|path| path.extension().is_some_and(|extension| extension == "app"))
            .ok_or_else(|| "Computer Use is not running from its installed macOS app bundle".into())
    }

    pub fn show(screen_recording: bool) -> Result<(), String> {
        let executable = std::env::current_exe().map_err(|error| error.to_string())?;
        let bundle = installed_bundle(&executable)?;
        let path = CString::new(bundle.as_os_str().as_bytes()).map_err(|error| error.to_string())?;
        if unsafe { crab_show_permission_drag_guide(path.as_ptr(), screen_recording) } {
            Ok(())
        } else {
            Err("Unable to show the Computer Use drag-to-authorize guide".into())
        }
    }

    pub fn hide_if_granted(capture_granted: bool, accessibility_granted: bool) {
        unsafe { crab_hide_permission_drag_guide_if_granted(capture_granted, accessibility_granted) }
    }
}

pub const PROTOCOL_VERSION: u32 = 1;
const REQUEST_LIMIT: u64 = 1024 * 1024;
const RESPONSE_LIMIT: u64 = 64 * 1024 * 1024;
const RPC_TIMEOUT: Duration = Duration::from_secs(95);

#[cfg(target_os = "macos")]
fn fresh_capabilities(executable: &Path, timeout: Duration) -> Result<Value, String> {
    // CGPreflightScreenCaptureAccess/AXIsProcessTrusted in a long-running
    // executor can retain a grant removed in System Settings. Probe the same
    // signed app in a fresh process without replacing the shared executor or
    // invalidating its leases and AX references.
    let mut child = Command::new(executable)
        .arg("--probe-capabilities")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("Unable to recheck Computer Use permissions: {error}"))?;
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(10));
            }
            result => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(match result {
                    Err(error) => format!("Unable to recheck Computer Use permissions: {error}"),
                    _ => "Computer Use permission check timed out; refresh to try again".into(),
                });
            }
        }
    }
    let output = child.wait_with_output().map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err("Computer Use permission check failed; refresh to try again".into());
    }
    let caps: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("Invalid Computer Use permission check result: {error}"))?;
    if !["gui_available", "input_available", "capture_available", "ax_available"]
        .iter().all(|key| caps[key].is_boolean())
    {
        return Err("Incomplete Computer Use permission check result".into());
    }
    Ok(caps)
}

fn detect_capabilities() -> Result<Value, String> {
    #[cfg(target_os = "macos")]
    return fresh_capabilities(
        &std::env::current_exe().map_err(|error| error.to_string())?,
        Duration::from_secs(10),
    );
    #[cfg(not(target_os = "macos"))]
    serde_json::to_value(crabcode_computer_use_core::detect_capabilities())
        .map_err(|error| error.to_string())
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Endpoint {
    pub protocol_version: u32,
    pub port: u16,
    pub token: String,
    pub instance_id: String,
    pub pid: u32,
}

pub fn state_dir() -> Result<PathBuf, String> {
    dirs::data_local_dir()
        .map(|p| p.join("CrabCode").join("ComputerUse"))
        .ok_or_else(|| "Cannot locate the user data directory".into())
}

pub fn installed_executable(dir: &Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    return dir.join("Crab Computer Use.app/Contents/MacOS/crabcode-computer-use-host");
    #[cfg(target_os = "windows")]
    return dir.join("crabcode-computer-use-host.exe");
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    return dir.join("crabcode-computer-use-host");
}

#[cfg(target_os = "macos")]
fn copy_tree(source: &Path, destination: &Path) -> Result<(), String> {
    std::fs::create_dir_all(destination).map_err(|e| e.to_string())?;
    for item in std::fs::read_dir(source).map_err(|e| e.to_string())? {
        let item = item.map_err(|e| e.to_string())?;
        let target = destination.join(item.file_name());
        if item.file_type().map_err(|e| e.to_string())?.is_dir() {
            copy_tree(&item.path(), &target)?;
        } else {
            std::fs::copy(item.path(), target).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// Both client distributions carry the same signed component. Install outside
/// versioned extension folders so upgrades do not change its permission path.
pub fn install(source: &Path, dir: &Path) -> Result<PathBuf, String> {
    private_dir(dir)?;
    let install_lock = private_file(&dir.join("install.lock"))?;
    install_lock.lock_exclusive().map_err(|e| e.to_string())?;
    let target = installed_executable(dir);
    if source == target {
        return Ok(target);
    }
    if !source.is_file() {
        return Err("The bundled Computer Use component is missing. Reinstall this client.".into());
    }
    if target.is_file()
        && std::fs::read(source).map_err(|e| e.to_string())?
            == std::fs::read(&target).map_err(|e| e.to_string())?
    {
        return Ok(target);
    }
    #[cfg(target_os = "macos")]
    if target.is_file() {
        // An older Desktop/VS Code/CLI bundle must not replace the newer app
        // just because its client started later. Ad-hoc signatures are tied to
        // the binary hash, so such a downgrade also invalidates TCC access.
        let source_modified = source.metadata().and_then(|meta| meta.modified());
        let installed_modified = target.metadata().and_then(|meta| meta.modified());
        if let (Ok(source_modified), Ok(installed_modified)) =
            (source_modified, installed_modified)
        {
            if source_modified <= installed_modified {
                return Ok(target);
            }
        }
    }
    // Do not replace a component while another client owns an active task.
    if let Ok(endpoint) = read_endpoint(dir) {
        if let Ok(info) = rpc(&endpoint, "ping", json!({})) {
            if info["build_id"] == env!("CRABCODE_COMPUTER_USE_BUILD") {
                return Ok(target);
            }
            rpc(&endpoint, "shutdown_if_idle", json!({})).map_err(|_| "Computer Use is in use or requires a restart. Finish active Computer Use tasks, then refresh the component.".to_string())?;
            let deadline = Instant::now() + Duration::from_secs(5);
            while rpc(&endpoint, "ping", json!({})).is_ok() {
                if Instant::now() >= deadline {
                    return Err("Previous Computer Use executor did not stop".into());
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        }
    }
    #[cfg(target_os = "macos")]
    {
        let bundle = source
            .parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .filter(|p| p.extension().is_some_and(|ext| ext == "app"))
            .ok_or("The component must include its signed macOS app bundle")?;
        let staging = dir.join(format!("install-{}.app", random_id()?));
        copy_tree(bundle, &staging)?;
        let installed = dir.join("Crab Computer Use.app");
        let backup = dir.join("previous.app");
        if backup.exists() {
            std::fs::remove_dir_all(&backup).map_err(|e| e.to_string())?;
        }
        if installed.exists() {
            std::fs::rename(&installed, &backup).map_err(|e| e.to_string())?;
        }
        if let Err(error) = std::fs::rename(&staging, &installed) {
            if backup.exists() {
                let _ = std::fs::rename(&backup, &installed);
            }
            return Err(error.to_string());
        }
        if backup.exists() {
            let _ = std::fs::remove_dir_all(backup);
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let staging = dir.join("install.tmp");
        std::fs::copy(source, &staging).map_err(|e| e.to_string())?;
        #[cfg(target_os = "windows")]
        if target.exists() {
            std::fs::remove_file(&target).map_err(|e| e.to_string())?;
        }
        std::fs::rename(staging, &target).map_err(|e| e.to_string())?;
    }
    Ok(target)
}

fn random_id() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| e.to_string())?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

fn private_dir(path: &Path) -> Result<(), String> {
    std::fs::create_dir_all(path).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn private_file(path: &Path) -> Result<File, String> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path).map_err(|e| e.to_string())
}

pub fn read_endpoint(dir: &Path) -> Result<Endpoint, String> {
    let bytes = std::fs::read(dir.join("endpoint.json"))
        .map_err(|_| "Computer Use executor is not running".to_string())?;
    let endpoint: Endpoint =
        serde_json::from_slice(&bytes).map_err(|e| format!("Invalid executor endpoint: {e}"))?;
    if endpoint.protocol_version != PROTOCOL_VERSION
        || endpoint.port == 0
        || endpoint.token.len() != 64
    {
        return Err("Incompatible Computer Use executor; update the Computer Use component".into());
    }
    Ok(endpoint)
}

pub fn rpc(endpoint: &Endpoint, method: &str, params: Value) -> Result<Value, String> {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, endpoint.port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2))
        .map_err(|e| format!("Computer Use executor connection failed: {e}"))?;
    stream
        .set_read_timeout(Some(RPC_TIMEOUT))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(10)))
        .map_err(|e| e.to_string())?;
    let message = json!({"token": endpoint.token, "instance_id": endpoint.instance_id,
        "method": method, "params": params, "deadline_ms": now_ms() + 85_000});
    serde_json::to_writer(&mut stream, &message).map_err(|e| e.to_string())?;
    stream.write_all(b"\n").map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    BufReader::new(stream.take(RESPONSE_LIMIT + 1))
        .read_until(b'\n', &mut bytes)
        .map_err(|e| format!("Computer Use reply was interrupted; observe before retrying: {e}"))?;
    if bytes.len() as u64 > RESPONSE_LIMIT || bytes.last() != Some(&b'\n') {
        return Err("Computer Use reply was truncated; observe before retrying".into());
    }
    let response: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    if response["ok"] != true {
        return Err(response["error"]
            .as_str()
            .unwrap_or("Computer Use executor failed")
            .into());
    }
    Ok(response["result"].clone())
}

/// Used only for starting/probing the host. Input RPCs are never retried.
pub fn ensure_running(dir: &Path, executable: &Path) -> Result<Endpoint, String> {
    private_dir(dir)?;
    if let Ok(endpoint) = read_endpoint(dir) {
        if rpc(&endpoint, "ping", json!({})).is_ok() {
            return Ok(endpoint);
        }
    }
    let startup = private_file(&dir.join("startup.lock"))?;
    let deadline = Instant::now() + Duration::from_secs(15);
    while startup.try_lock_exclusive().is_err() {
        if Instant::now() >= deadline {
            return Err("Timed out starting the Computer Use executor".into());
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    if let Ok(endpoint) = read_endpoint(dir) {
        if rpc(&endpoint, "ping", json!({})).is_ok() {
            return Ok(endpoint);
        }
    }
    if !executable.is_file() {
        return Err(format!(
            "Computer Use component is not installed: {}",
            executable.display()
        ));
    }
    #[cfg(target_os = "macos")]
    let mut child = {
        // Launch the app bundle through LaunchServices so the helper owns its
        // permission identity and its main AppKit loop, independent of either UI.
        let bundle = executable
            .parent()
            .and_then(Path::parent)
            .and_then(Path::parent)
            .filter(|p| p.extension().is_some_and(|e| e == "app"))
            .ok_or("The macOS executor must be installed as an app bundle")?;
        let mut command = Command::new("/usr/bin/open");
        command
            // After an idle upgrade LaunchServices can still know the exiting
            // app. Start a fresh instance; executor.lock enforces the singleton.
            .arg("-n")
            .arg("-g")
            .arg("-a")
            .arg(bundle)
            .arg("--args")
            .arg("--serve")
            .arg("--state-dir")
            .arg(dir);
        command
    };
    #[cfg(not(target_os = "macos"))]
    let mut child = {
        let mut command = Command::new(executable);
        command.arg("--serve").arg("--state-dir").arg(dir);
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        command
    };
    child
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| e.to_string())?;
    while Instant::now() < deadline {
        if let Ok(endpoint) = read_endpoint(dir) {
            if rpc(&endpoint, "ping", json!({})).is_ok() {
                return Ok(endpoint);
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Err("Computer Use executor did not become ready. Check the component installation and system permissions.".into())
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[derive(Default)]
struct NativeState {
    // Heartbeats keep AX owners alive while the model is thinking. A crashed
    // frontend loses its references, but never causes an input action to replay.
    owners: std::collections::HashMap<(String, String), Instant>,
    leases: std::collections::HashSet<(String, String, String, Option<String>)>,
}

impl NativeState {
    fn touch(&mut self, host: &str, connection: &str) {
        if !host.is_empty() && !connection.is_empty() {
            self.owners
                .insert((host.into(), connection.into()), Instant::now());
        }
    }

    fn reap(&mut self) {
        self.owners.retain(|(host, connection), last_seen| {
            if last_seen.elapsed() < Duration::from_secs(120) {
                return true;
            }
            crabcode_computer_use_core::release_ax(host, connection, None, None, true);
            false
        });
        self.leases.retain(|(host, connection, _, _)| {
            self.owners
                .contains_key(&(host.clone(), connection.clone()))
        });
    }
}

fn dispatch(
    method: &str,
    params: &Value,
    instance: &str,
    state: &mut NativeState,
    queued: Instant,
) -> Result<Value, String> {
    let text = |key: &str| params[key].as_str().unwrap_or("");
    match method {
        "capabilities" => {
            let mut caps = detect_capabilities()?;
            #[cfg(target_os = "macos")]
            permission_guide::hide_if_granted(caps["capture_available"] == true, caps["ax_available"] == true);
            caps["instance_id"] = json!(instance);
            caps["environment"] = json!("host");
            caps["host_protocol_version"] = json!(PROTOCOL_VERSION);
            Ok(caps)
        }
        "execute" => {
            let request = params["request"].clone();
            let parsed = serde_json::from_value(request.clone())
                .map_err(|e| format!("Invalid Computer Use request: {e}"))?;
            if params["instanceId"].as_str() != Some(instance) {
                return Err("Computer Use executor restarted. Refresh capabilities and observe before acting.".into());
            }
            if let Some(owner) = params.get("lease").or_else(|| request.get("owner")) {
                state.touch(
                    owner["host_id"].as_str().unwrap_or(""),
                    owner["connection_id"].as_str().unwrap_or(""),
                );
                state.leases.insert((
                    owner["host_id"].as_str().unwrap_or("").into(),
                    owner["connection_id"].as_str().unwrap_or("").into(),
                    owner["session_id"].as_str().unwrap_or("").into(),
                    owner["agent_id"].as_str().map(str::to_string),
                ));
            }
            let queue_ms = queued.elapsed().as_secs_f64() * 1000.0;
            crabcode_computer_use_core::execute(parsed).map(|mut result| {
                result["timings_ms"]["worker_queue"] = json!(queue_ms);
                result
            })
        }
        "release" => {
            crabcode_computer_use_core::release_ax(
                text("hostId"),
                text("connectionId"),
                params["sessionId"].as_str(),
                params["agentId"].as_str(),
                params["allAgents"] == true,
            );
            if params["sessionId"].is_null() {
                state
                    .owners
                    .remove(&(text("hostId").into(), text("connectionId").into()));
            }
            state.leases.retain(|(host, connection, session, agent)| {
                !(host == text("hostId")
                    && connection == text("connectionId")
                    && (params["sessionId"].is_null() || params["sessionId"] == session.as_str())
                    && (params["allAgents"] == true
                        || agent.as_deref() == params["agentId"].as_str()))
            });
            Ok(Value::Null)
        }
        "heartbeat" => {
            state.touch(text("hostId"), text("connectionId"));
            Ok(Value::Null)
        }
        "permissions" => {
            let screen_recording_pane =
                crabcode_computer_use_core::open_input_settings(params["kind"].as_str())?;
            #[cfg(target_os = "macos")]
            permission_guide::show(screen_recording_pane)?;
            Ok(json!({"drag_guide_visible": true, "kind": if screen_recording_pane { "screen_recording" } else { "accessibility" }}))
        }
        _ => Err("Unknown Computer Use executor method".into()),
    }
}

fn serve_connection(
    mut stream: TcpStream,
    endpoint: &Endpoint,
    state: &Mutex<NativeState>,
    stopping: &AtomicBool,
) -> Result<bool, String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(15)))
        .map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    BufReader::new((&stream).take(REQUEST_LIMIT + 1))
        .read_until(b'\n', &mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > REQUEST_LIMIT || bytes.last() != Some(&b'\n') {
        return Err("Invalid executor message".into());
    }
    let message: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    let mut shutdown = false;
    let result = if message["token"].as_str() != Some(&endpoint.token)
        || message["instance_id"].as_str() != Some(&endpoint.instance_id)
    {
        Err("Unauthorized executor connection".into())
    } else if message["method"] == "ping" {
        Ok(
            json!({"protocol_version": PROTOCOL_VERSION, "version": env!("CARGO_PKG_VERSION"), "build_id": env!("CRABCODE_COMPUTER_USE_BUILD"), "instance_id": endpoint.instance_id}),
        )
    } else if message["method"] == "shutdown_if_idle" {
        match state.try_lock() {
            Ok(mut native) => {
                native.reap();
                if native.leases.is_empty() {
                    stopping.store(true, Ordering::SeqCst);
                    shutdown = true;
                    Ok(Value::Null)
                } else {
                    Err("Computer Use has an active task".into())
                }
            }
            Err(_) => Err("Computer Use is executing an action".into()),
        }
    } else {
        let queued = Instant::now();
        let mut native = state
            .lock()
            .map_err(|_| "Native executor stopped after an internal failure")?;
        stream.set_nonblocking(true).map_err(|e| e.to_string())?;
        let disconnected = matches!(stream.peek(&mut [0u8; 1]), Ok(0));
        stream.set_nonblocking(false).map_err(|e| e.to_string())?;
        if stopping.load(Ordering::SeqCst)
            || disconnected
            || message["deadline_ms"]
                .as_u64()
                .is_none_or(|deadline| deadline < now_ms())
        {
            Err("Computer Use request expired before dispatch".into())
        } else {
            native.reap();
            dispatch(
                message["method"].as_str().unwrap_or(""),
                &message["params"],
                &endpoint.instance_id,
                &mut native,
                queued,
            )
        }
    };
    let response = match result {
        Ok(value) => json!({"ok": true, "result": value}),
        Err(error) => json!({"ok": false, "error": error}),
    };
    serde_json::to_writer(&mut stream, &response).map_err(|e| e.to_string())?;
    stream.write_all(b"\n").map_err(|e| e.to_string())?;
    Ok(shutdown)
}

pub fn serve(dir: PathBuf) -> Result<(), String> {
    private_dir(&dir)?;
    let lock = private_file(&dir.join("executor.lock"))?;
    lock.try_lock_exclusive()
        .map_err(|_| "Computer Use executor is already running".to_string())?;
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).map_err(|e| e.to_string())?;
    let endpoint = Endpoint {
        protocol_version: PROTOCOL_VERSION,
        port: listener.local_addr().map_err(|e| e.to_string())?.port(),
        token: random_id()?,
        instance_id: random_id()?,
        pid: std::process::id(),
    };
    let temp_path = dir.join(format!("endpoint-{}.json", endpoint.instance_id));
    let mut file = private_file(&temp_path)?;
    serde_json::to_writer(&mut file, &endpoint).map_err(|e| e.to_string())?;
    file.sync_all().map_err(|e| e.to_string())?;
    #[cfg(target_os = "windows")]
    let _ = std::fs::remove_file(dir.join("endpoint.json"));
    std::fs::rename(temp_path, dir.join("endpoint.json")).map_err(|e| e.to_string())?;
    let state = Arc::new(Mutex::new(NativeState::default()));
    let stopping = Arc::new(AtomicBool::new(false));
    let connections = Arc::new(AtomicUsize::new(0));
    let cleanup = state.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(30));
        if let Ok(mut native) = cleanup.lock() {
            native.reap();
        }
    });
    let server = move || {
        let _lock = lock; // Keep the per-user singleton lock for the process lifetime.
        for stream in listener.incoming().flatten() {
            if connections.fetch_add(1, Ordering::SeqCst) >= 64 {
                connections.fetch_sub(1, Ordering::SeqCst);
                continue;
            }
            let state = state.clone();
            let endpoint = endpoint.clone();
            let stopping = stopping.clone();
            let connections = connections.clone();
            std::thread::spawn(move || {
                let shutdown =
                    serve_connection(stream, &endpoint, &state, &stopping).unwrap_or(false);
                connections.fetch_sub(1, Ordering::SeqCst);
                if shutdown {
                    std::thread::sleep(Duration::from_millis(100));
                    std::process::exit(0);
                }
            });
        }
    };
    #[cfg(target_os = "macos")]
    {
        std::thread::spawn(server);
        let mtm =
            objc2::MainThreadMarker::new().ok_or("Computer Use must start on the main thread")?;
        let app = objc2_app_kit::NSApplication::sharedApplication(mtm);
        app.setActivationPolicy(objc2_app_kit::NSApplicationActivationPolicy::Accessory);
        app.run();
    }
    #[cfg(not(target_os = "macos"))]
    server();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "macos")]
    fn probe_script(path: &Path, body: &str) {
        use std::os::unix::fs::PermissionsExt;
        std::fs::write(path, format!("#!/bin/sh\n[ \"$1\" = --probe-capabilities ] || exit 3\n{body}\n")).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn fresh_permission_probe_does_not_reuse_previous_grants() {
        let dir = tempfile::tempdir().unwrap();
        let executable = dir.path().join("probe");
        for granted in [true, false, true] {
            let caps = json!({"gui_available": granted, "input_available": granted,
                "capture_available": granted, "ax_available": granted});
            probe_script(&executable, &format!("printf '%s\\n' '{caps}'"));
            assert_eq!(fresh_capabilities(&executable, Duration::from_secs(2)).unwrap(), caps);
        }
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn failed_or_incomplete_probes_cannot_report_cached_grants() {
        let dir = tempfile::tempdir().unwrap();
        let executable = dir.path().join("probe");
        for body in ["exit 1", "echo invalid-json", "echo '{}'", "echo '{\"gui_available\":true}'"] {
            probe_script(&executable, body);
            assert!(fresh_capabilities(&executable, Duration::from_secs(2)).is_err());
        }
        probe_script(&executable, "exec /bin/sleep 30");
        let started = Instant::now();
        assert!(fresh_capabilities(&executable, Duration::from_millis(40))
            .unwrap_err().contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn permission_guide_uses_the_executing_app_bundle() {
        let app = Path::new("/tmp/Crab Computer Use.app");
        let executable = app.join("Contents/MacOS/crabcode-computer-use-host");
        assert_eq!(permission_guide::installed_bundle(&executable).unwrap(), app);
        assert!(permission_guide::installed_bundle(Path::new("/tmp/crabcode-computer-use-host")).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn invalid_permission_kind_is_rejected_without_opening_system_settings() {
        let mut state = NativeState::default();
        let error = dispatch(
            "permissions", &json!({"kind":"other"}), "instance", &mut state, Instant::now(),
        ).unwrap_err();
        assert!(error.contains("Unknown Computer Use permission kind"));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn older_client_cannot_replace_a_newer_shared_app() {
        let temp = tempfile::tempdir().unwrap();
        let source = |client: &str, contents: &str, modified: SystemTime| {
            let executable = temp.path().join(client).join(
                "Crab Computer Use.app/Contents/MacOS/crabcode-computer-use-host",
            );
            std::fs::create_dir_all(executable.parent().unwrap()).unwrap();
            std::fs::write(&executable, contents).unwrap();
            File::options().write(true).open(&executable).unwrap()
                .set_modified(modified).unwrap();
            executable
        };
        let now = SystemTime::now();
        let older = source("vscode", "older", now - Duration::from_secs(20));
        let newer = source("desktop", "newer", now - Duration::from_secs(10));
        let newest = source("companion", "newest", now);
        let state = temp.path().join("state");
        let installed = install(&older, &state).unwrap();
        assert_eq!(std::fs::read(&installed).unwrap(), b"older");
        install(&newer, &state).unwrap();
        assert_eq!(std::fs::read(&installed).unwrap(), b"newer");
        install(&older, &state).unwrap();
        assert_eq!(std::fs::read(&installed).unwrap(), b"newer");
        install(&newest, &state).unwrap();
        assert_eq!(std::fs::read(&installed).unwrap(), b"newest");
    }

    fn test_server() -> (Endpoint, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let endpoint = Endpoint {
            protocol_version: 1,
            port: listener.local_addr().unwrap().port(),
            token: "a".repeat(64),
            instance_id: "instance".into(),
            pid: 1,
        };
        let server_endpoint = endpoint.clone();
        let thread = std::thread::spawn(move || {
            serve_connection(
                listener.accept().unwrap().0,
                &server_endpoint,
                &Mutex::new(NativeState::default()),
                &AtomicBool::new(false),
            )
            .unwrap();
        });
        (endpoint, thread)
    }

    #[test]
    fn rejects_wrong_token_before_native_dispatch() {
        let (mut endpoint, thread) = test_server();
        endpoint.token = "b".repeat(64);
        assert!(rpc(&endpoint, "execute", json!({}))
            .unwrap_err()
            .contains("Unauthorized"));
        thread.join().unwrap();
    }

    #[test]
    fn ping_and_protocol_round_trip() {
        let (endpoint, thread) = test_server();
        assert_eq!(
            rpc(&endpoint, "ping", json!({})).unwrap()["protocol_version"],
            1
        );
        thread.join().unwrap();
    }

    #[test]
    fn restarted_instance_cannot_receive_stale_input() {
        let (mut endpoint, thread) = test_server();
        endpoint.instance_id = "previous-instance".into();
        assert!(rpc(&endpoint, "execute", json!({}))
            .unwrap_err()
            .contains("Unauthorized"));
        thread.join().unwrap();
    }

    #[test]
    fn valid_request_keeps_native_policy_rejection() {
        let request = json!({"target_scope":"desktop", "delivery_policy":"strict_background", "action":{"action":"click","x":1,"y":1}});
        let original =
            crabcode_computer_use_core::execute(serde_json::from_value(request.clone()).unwrap())
                .unwrap();
        let (endpoint, thread) = test_server();
        let actual = rpc(
            &endpoint,
            "execute",
            json!({"instanceId":"instance", "request":request}),
        )
        .unwrap();
        thread.join().unwrap();
        for field in [
            "ok",
            "error",
            "error_code",
            "action_dispatched",
            "dispatch_succeeded",
            "retry_safe",
            "focus_isolation",
        ] {
            assert_eq!(
                actual[field], original[field],
                "changed native receipt field {field}"
            );
        }
    }

    fn state_server(
        state: NativeState,
    ) -> (
        Endpoint,
        Arc<Mutex<NativeState>>,
        Arc<AtomicBool>,
        std::thread::JoinHandle<bool>,
    ) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let endpoint = Endpoint {
            protocol_version: 1,
            port: listener.local_addr().unwrap().port(),
            token: "a".repeat(64),
            instance_id: "instance".into(),
            pid: 1,
        };
        let state = Arc::new(Mutex::new(state));
        let stopping = Arc::new(AtomicBool::new(false));
        let (native, stop, address) = (state.clone(), stopping.clone(), endpoint.clone());
        let thread = std::thread::spawn(move || {
            serve_connection(listener.accept().unwrap().0, &address, &native, &stop).unwrap()
        });
        (endpoint, state, stopping, thread)
    }

    #[test]
    fn component_upgrade_waits_for_all_active_sessions() {
        let mut native = NativeState::default();
        native.touch("host", "connection");
        native
            .leases
            .insert(("host".into(), "connection".into(), "session".into(), None));
        let (endpoint, _, stopping, thread) = state_server(native);
        assert!(rpc(&endpoint, "shutdown_if_idle", json!({}))
            .unwrap_err()
            .contains("active task"));
        assert!(!thread.join().unwrap());
        assert!(!stopping.load(Ordering::SeqCst));
        let (endpoint, _, stopping, thread) = state_server(NativeState::default());
        rpc(&endpoint, "shutdown_if_idle", json!({})).unwrap();
        assert!(thread.join().unwrap());
        assert!(stopping.load(Ordering::SeqCst));
    }

    #[test]
    fn releasing_one_agent_preserves_other_sessions_and_clients() {
        let mut native = NativeState::default();
        for (host, session, agent) in [
            ("host", "one", None),
            ("host", "one", Some("child")),
            ("host", "two", None),
            ("other", "one", None),
        ] {
            native.touch(host, "connection");
            native.leases.insert((
                host.into(),
                "connection".into(),
                session.into(),
                agent.map(str::to_string),
            ));
        }
        dispatch("release", &json!({"hostId":"host", "connectionId":"connection", "sessionId":"one", "agentId":"child", "allAgents":false}), "instance", &mut native, Instant::now()).unwrap();
        assert_eq!(native.leases.len(), 3);
        assert!(native
            .leases
            .contains(&("host".into(), "connection".into(), "one".into(), None)));
        dispatch(
            "release",
            &json!({"hostId":"host", "connectionId":"connection", "allAgents":true}),
            "instance",
            &mut native,
            Instant::now(),
        )
        .unwrap();
        assert_eq!(native.leases.len(), 1);
        assert_eq!(native.leases.iter().next().unwrap().0, "other");
    }

    #[test]
    fn crashed_clients_expire_without_releasing_live_clients() {
        let mut native = NativeState::default();
        native.owners.insert(
            ("crashed".into(), "connection".into()),
            Instant::now() - Duration::from_secs(121),
        );
        native.touch("live", "connection");
        for host in ["crashed", "live"] {
            native
                .leases
                .insert((host.into(), "connection".into(), "session".into(), None));
        }
        native.reap();
        assert_eq!(native.owners.len(), 1);
        assert_eq!(native.leases.len(), 1);
        assert_eq!(native.leases.iter().next().unwrap().0, "live");
    }

    #[test]
    fn expired_request_does_not_create_a_native_lease() {
        let (endpoint, state, _, thread) = state_server(NativeState::default());
        let mut stream = TcpStream::connect((Ipv4Addr::LOCALHOST, endpoint.port)).unwrap();
        let message = json!({"token":endpoint.token,"instance_id":endpoint.instance_id,"method":"heartbeat",
            "params":{"hostId":"host","connectionId":"connection"},"deadline_ms":0});
        writeln!(stream, "{message}").unwrap();
        let mut response = String::new();
        BufReader::new(stream).read_line(&mut response).unwrap();
        assert!(response.contains("expired before dispatch"));
        thread.join().unwrap();
        assert!(state.lock().unwrap().owners.is_empty());
    }
}
