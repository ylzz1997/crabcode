//! Local Lume VMs only. GUI input never falls back to the host executor.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::net::Ipv4Addr;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

#[cfg(target_os = "macos")]
mod forwards;
#[cfg(target_os = "macos")]
mod guest;
pub mod installer;

const RESPONSE_LIMIT: u64 = 32 * 1024 * 1024;
#[cfg(target_os = "macos")]
const APP: &str = "Crab Computer Use.app";

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct VmConfig {
    pub name: String,
    pub user: String,
    pub storage: String,
    pub shared_directory: String,
    pub shared_read_only: bool,
    pub forwarded_ports: Vec<u16>,
}
impl Default for VmConfig {
    fn default() -> Self {
        Self {
            name: "crabcode".into(),
            user: "lume".into(),
            storage: "default".into(),
            shared_directory: String::new(),
            shared_read_only: true,
            forwarded_ports: vec![],
        }
    }
}
impl VmConfig {
    fn validate(&self) -> Result<(), String> {
        for (label, value) in [
            ("VM name", &self.name),
            ("VM user", &self.user),
            ("Storage", &self.storage),
        ] {
            if value.is_empty()
                || value.len() > 80
                || value.starts_with('-')
                || matches!(value.as_str(), "." | "..")
                || !value
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"_-.".contains(&c))
            {
                return Err(format!(
                    "{label} must use letters, numbers, dots, underscores or hyphens"
                ));
            }
        }
        if self.shared_directory.contains(['\n', '\r', '\0', ':']) {
            return Err("Shared directory cannot contain colons or control characters".into());
        }
        let unique: std::collections::HashSet<_> = self.forwarded_ports.iter().collect();
        if self.forwarded_ports.len() > 8
            || unique.len() != self.forwarded_ports.len()
            || self.forwarded_ports.contains(&0)
        {
            return Err("Choose up to eight distinct TCP ports between 1 and 65535".into());
        }
        Ok(())
    }
    fn environment_id(&self) -> String {
        format!("lume:{}:{}", self.storage, self.name)
    }
}

fn supported() -> Result<(), String> {
    if !cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        return Err("Local macOS VMs require an Apple Silicon Mac".into());
    }
    Ok(())
}

fn state_dir() -> Result<PathBuf, String> {
    let path = dirs::home_dir()
        .ok_or("Home directory is unavailable")?
        .join(".crabcode/vm");
    std::fs::create_dir_all(&path).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))
            .map_err(|e| e.to_string())?;
    }
    Ok(path)
}

fn lume() -> Result<PathBuf, String> {
    supported()?;
    let mut paths = vec![
        PathBuf::from("/opt/homebrew/bin/lume"),
        PathBuf::from("/usr/local/bin/lume"),
    ];
    if let Some(home) = dirs::home_dir() {
        paths.insert(0, home.join(".local/bin/lume"));
    }
    if let Some(path) = std::env::var_os("PATH") {
        paths.extend(std::env::split_paths(&path).map(|p| p.join("lume")));
    }
    paths.into_iter().find(|p| p.is_file()).ok_or_else(|| "Lume is not installed. Install Lume 0.5.3 or later from https://cua.ai/docs/how-to-guides/lume/install-lume".into())
}

// Read both pipes while a command runs: screenshots exceed pipe capacity. Every
// external operation has a deadline, including failed SSH and installation.
fn output(mut command: Command, input: Vec<u8>, timeout: Duration) -> Result<Vec<u8>, String> {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command
        .spawn()
        .map_err(|e| format!("Could not start VM command: {e}"))?;
    let mut stdin = child.stdin.take().ok_or("Missing stdin")?;
    let stdout = child.stdout.take().ok_or("Missing stdout")?;
    let stderr = child.stderr.take().ok_or("Missing stderr")?;
    std::thread::spawn(move || {
        let _ = stdin.write_all(&input);
    });
    let (data_tx, data_rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout
            .take(RESPONSE_LIMIT + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes);
        let _ = data_tx.send(result);
    });
    let (error_tx, error_rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stderr
            .take(1024 * 1024)
            .read_to_end(&mut bytes)
            .map(|_| bytes);
        let _ = error_tx.send(result);
    });
    let start = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
            break status;
        }
        if start.elapsed() > timeout {
            #[cfg(target_os = "macos")]
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
            // Closing all local SSH descriptors also closes its guest socket.
            return Err("VM command timed out; an input may have arrived. Reconnect and observe before continuing.".into());
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    let bytes = data_rx
        .recv_timeout(timeout.saturating_sub(start.elapsed()))
        .map_err(|_| "VM command left its output pipe open past the deadline")?
        .map_err(|e| e.to_string())?;
    let err = error_rx
        .recv_timeout(timeout.saturating_sub(start.elapsed()))
        .map_err(|_| "VM command left its error pipe open past the deadline")?
        .map_err(|e| e.to_string())?;
    if !status.success() {
        let message = String::from_utf8_lossy(&err);
        // Lume stdout can contain VNC credentials. Never include it in errors.
        return Err(format!(
            "VM command failed ({status}): {}",
            message.chars().take(1500).collect::<String>()
        ));
    }
    if bytes.len() as u64 > RESPONSE_LIMIT {
        return Err("VM response is too large".into());
    }
    Ok(bytes)
}

fn lume_command(config: &VmConfig, action: &str) -> Result<Command, String> {
    config.validate()?;
    let mut c = Command::new(lume()?);
    c.arg(action)
        .arg(&config.name)
        .arg("--storage")
        .arg(&config.storage);
    Ok(c)
}

fn vm_info(config: &VmConfig) -> Result<Value, String> {
    let mut c = lume_command(config, "get")?;
    c.args(["--format", "json"]);
    let value: Value = serde_json::from_slice(&output(c, vec![], Duration::from_secs(15))?)
        .map_err(|e| format!("Invalid Lume JSON: {e}"))?;
    let info = if let Some(items) = value.as_array() {
        items
            .iter()
            .find(|v| v["name"] == config.name)
            .cloned()
            .ok_or("VM was not found")?
    } else {
        value
    };
    if info["name"] != config.name {
        return Err("Lume returned a different VM".into());
    }
    if !info["os"]
        .as_str()
        .unwrap_or("macOS")
        .eq_ignore_ascii_case("macos")
    {
        return Err("Only macOS guests are supported".into());
    }
    Ok(info)
}

fn guest_address(info: &Value) -> Result<String, String> {
    if !info["status"]
        .as_str()
        .unwrap_or("")
        .eq_ignore_ascii_case("running")
    {
        return Err("Start the VM before connecting".into());
    }
    let address = info["ipAddress"]
        .as_str()
        .or_else(|| info["ip_address"].as_str())
        .ok_or("VM has no IP address yet; wait for macOS to boot")?;
    let ip: Ipv4Addr = address
        .parse()
        .map_err(|_| "Lume returned an invalid IPv4 address")?;
    if !ip.is_private() || ip.is_loopback() {
        return Err("Only a local VM on a private network is supported".into());
    }
    Ok(ip.to_string())
}

#[cfg(any(test, target_os = "macos"))]
fn quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn ssh_options(config: &VmConfig) -> Result<Command, String> {
    config.validate()?;
    let state = state_dir()?;
    let mut c = Command::new("/usr/bin/ssh");
    c.args(["-F", "/dev/null"]);
    c.args([
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
    ]);
    c.arg("-o").arg(format!(
        "UserKnownHostsFile={}",
        state.join("known_hosts").display()
    ));
    c.arg("-o").arg(format!(
        "HostKeyAlias=crab-{}-{}",
        config.storage, config.name
    ));
    c.arg("-i").arg(state.join("id_ed25519"));
    Ok(c)
}

fn ssh(config: &VmConfig, address: &str) -> Result<Command, String> {
    let mut c = ssh_options(config)?;
    c.arg(format!("{}@{}", config.user, address));
    Ok(c)
}

fn rpc(config: &VmConfig, request: Value) -> Result<Value, String> {
    let ip = guest_address(&vm_info(config)?)?;
    let mut c = ssh(config, &ip)?;
    c.arg("/usr/bin/nc -U \"$HOME/Library/Application Support/CrabComputerUse/control.sock\"");
    let mut input = serde_json::to_vec(&request).map_err(|e| e.to_string())?;
    input.push(b'\n');
    let data = output(c, input, Duration::from_secs(75))?;
    serde_json::from_slice(&data).map_err(|_| {
        "VM executor did not return a valid response; install it and log in to the VM desktop"
            .into()
    })
}

fn capabilities(config: &VmConfig) -> Result<Value, String> {
    let mut value = rpc(config, json!({"method": "capabilities"}))?;
    if value["isolated_vm"] != true || value["instance_id"].as_str().unwrap_or("").is_empty() {
        return Err("The executor did not identify an isolated macOS VM".into());
    }
    value["environment"] = json!("local_vm");
    value["environment_id"] = json!(config.environment_id());
    value["environment_name"] = json!(config.name);
    value["shared_directory"] = json!(config.shared_directory);
    value["shared_read_only"] = json!(config.shared_read_only);
    value["forwarded_ports"] = json!(config.forwarded_ports);
    value["supported_modes"] = json!(["foreground_desktop"]);
    #[cfg(target_os = "macos")]
    forwards::ensure(config)?;
    Ok(value)
}

#[tauri::command]
pub async fn computer_use_vm_list(storage: Option<String>) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let config = VmConfig {
            storage: storage.unwrap_or_else(|| "default".into()),
            ..Default::default()
        };
        config.validate()?;
        let mut c = Command::new(lume()?);
        c.args(["ls", "--format", "json", "--storage", &config.storage]);
        let values: Value = serde_json::from_slice(&output(c, vec![], Duration::from_secs(15))?)
            .map_err(|e| e.to_string())?;
        let items = values.as_array().ok_or("Invalid Lume VM list")?;
        Ok(json!(items
            .iter()
            .map(|v| json!({"name":v["name"],"status":v["status"],"os":v["os"]}))
            .collect::<Vec<_>>()))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn computer_use_vm_capabilities(config: VmConfig) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || capabilities(&config))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn computer_use_vm_execute(
    config: VmConfig,
    instance_id: String,
    owner: String,
    request: Value,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if instance_id.is_empty() {
            return Err("Reconnect to the VM before executing actions".into());
        }
        let mut result = rpc(
            &config,
            json!({"method":"execute", "instance_id":instance_id,"owner":owner,"request":request}),
        )?;
        result["environment"] = json!("local_vm");
        result["environment_id"] = json!(config.environment_id());
        Ok(result)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn computer_use_vm_release(
    config: VmConfig,
    instance_id: String,
    owner: String,
    all_agents: bool,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || rpc(&config, json!({"method":"release","instance_id":instance_id,"owner":owner,"all_agents":all_agents}))).await.map_err(|e| e.to_string())?
}

#[derive(Deserialize)]
pub struct VmCreateOptions {
    pub cpus: u32,
    pub memory_gb: u32,
    pub disk_gb: u32,
    pub ipsw: String,
}

#[tauri::command]
pub async fn computer_use_vm_manage(
    config: VmConfig,
    operation: String,
    password: Option<String>,
    create: Option<VmCreateOptions>,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        supported()?;
        config.validate()?;
        match operation.as_str() {
            "start" => {
                let mut c = lume_command(&config, "run")?;
                c.args(["--detach", "--display", "none"]);
                if !config.shared_directory.is_empty() {
                    let path = expand_path(&config.shared_directory)?;
                    if !path.is_dir() {
                        return Err("Shared directory does not exist".into());
                    }
                    c.arg("--shared-dir").arg(format!(
                        "{}:{}",
                        path.display(),
                        if config.shared_read_only { "ro" } else { "rw" }
                    ));
                }
                output(c, vec![], Duration::from_secs(120))?;
            }
            "stop" => {
                output(
                    lume_command(&config, "stop")?,
                    vec![],
                    Duration::from_secs(30),
                )?;
                #[cfg(target_os = "macos")]
                forwards::stop(&config);
            }
            "view" => {
                // Native Lume viewers automatically synchronize the clipboard.
                // Use Screen Sharing for explicit manual setup/takeover only.
                let info = vm_info(&config)?;
                let address = guest_address(&info)?;
                let url = url::Url::parse(
                    info["vncUrl"]
                        .as_str()
                        .ok_or("This VM has no VNC viewer; start it with VNC enabled")?,
                )
                .map_err(|_| "Invalid VM viewer address")?;
                if url.scheme() != "vnc"
                    || !matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "::1"))
                        && url.host_str() != Some(address.as_str())
                {
                    return Err("The viewer must connect to this local VM".into());
                }
                let mut c = Command::new("/usr/bin/open");
                c.arg(url.as_str());
                output(c, vec![], Duration::from_secs(20))?;
            }
            "create" => {
                let options = create.ok_or("Missing VM resources")?;
                if !(2..=32).contains(&options.cpus)
                    || !(4..=128).contains(&options.memory_gb)
                    || !(50..=2048).contains(&options.disk_gb)
                {
                    return Err(
                        "Invalid VM resources (CPU 2–32, memory 4–128 GiB, disk 50–2048 GiB)"
                            .into(),
                    );
                }
                if options.ipsw.trim().is_empty() {
                    return Err("An IPSW path or latest is required".into());
                }
                let mut c = lume_command(&config, "create")?;
                c.args([
                    "--os",
                    "macOS",
                    "--cpu",
                    &options.cpus.to_string(),
                    "--memory",
                    &format!("{}GB", options.memory_gb),
                    "--disk-size",
                    &format!("{}GB", options.disk_gb),
                    "--display",
                    "1440x900",
                    "--ipsw",
                    &options.ipsw,
                    "--unattended",
                    "tahoe",
                ]);
                output(c, vec![], Duration::from_secs(7200))?;
            }
            "install" => {
                install_guest(&config, password.as_deref())?;
            }
            "permissions" => {
                return rpc(&config, json!({"method":"permissions"}));
            }
            "takeover" => {
                return rpc(&config, json!({"method":"takeover"}));
            }
            "resume" => {
                return rpc(&config, json!({"method":"resume"}));
            }
            _ => return Err("Unknown VM operation".into()),
        }
        Ok(json!({"ok":true}))
    })
    .await
    .map_err(|e| e.to_string())?
}

fn expand_path(value: &str) -> Result<PathBuf, String> {
    if let Some(relative) = value.strip_prefix("~/") {
        Ok(dirs::home_dir()
            .ok_or("Home directory is unavailable")?
            .join(relative))
    } else if Path::new(value).is_absolute() {
        Ok(PathBuf::from(value))
    } else {
        Err("Use an absolute shared directory path".into())
    }
}

fn install_guest(config: &VmConfig, password: Option<&str>) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        guest::install(config, password)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (config, password);
        Err("Local VMs require macOS".into())
    }
}

pub fn run_guest_if_requested() -> bool {
    if std::env::args().nth(1).as_deref() != Some("--computer-use-guest") {
        return false;
    }
    #[cfg(target_os = "macos")]
    if let Err(error) = guest::run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
    true
}

pub(crate) fn stop_forwards() {
    #[cfg(target_os = "macos")]
    forwards::stop_all();
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn no_remote_or_shell_targets() {
        for name in [
            "../host",
            "-oProxyCommand=x",
            "x;open",
            "x\ny",
            "user@host",
            "",
        ] {
            assert!(VmConfig {
                name: name.into(),
                ..Default::default()
            }
            .validate()
            .is_err());
        }
        assert!(guest_address(&json!({"status":"running","ipAddress":"127.0.0.1"})).is_err());
        assert!(guest_address(&json!({"status":"running","ipAddress":"8.8.8.8"})).is_err());
        assert_eq!(
            guest_address(&json!({"status":"running","ipAddress":"192.168.64.5"})).unwrap(),
            "192.168.64.5"
        );
        assert!(guest_address(&json!({"status":"stopped","ipAddress":"192.168.64.5"})).is_err());
    }
    #[cfg(target_os = "macos")]
    #[test]
    fn netcat_transport_reads_a_large_reply_after_request_eof() {
        use std::io::{BufRead, BufReader};
        use std::os::unix::net::UnixListener;
        let dir = tempfile::Builder::new()
            .prefix("crab-rpc-")
            .tempdir_in("/tmp")
            .unwrap();
        let socket = dir.path().join("rpc.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut line = String::new();
            BufReader::new(&stream).read_line(&mut line).unwrap();
            assert_eq!(line, "request\n");
            stream.write_all(&vec![b'x'; 200_000]).unwrap();
        });
        let mut command = Command::new("/usr/bin/nc");
        command.arg("-U").arg(&socket);
        let result = output(command, b"request\n".to_vec(), Duration::from_secs(5)).unwrap();
        assert_eq!(result.len(), 200_000);
        worker.join().unwrap();
    }
    #[cfg(target_os = "macos")]
    #[test]
    fn blocked_transport_has_a_bounded_deadline() {
        let mut command = Command::new("/bin/sleep");
        command.arg("10");
        let start = Instant::now();
        assert!(output(command, vec![], Duration::from_millis(100))
            .unwrap_err()
            .contains("timed out"));
        assert!(start.elapsed() < Duration::from_secs(2));
    }
    #[test]
    fn forwarded_ports_are_explicit_and_bounded() {
        for ports in [vec![0], vec![3000, 3000], (1..=9).collect()] {
            assert!(VmConfig {
                forwarded_ports: ports,
                ..Default::default()
            }
            .validate()
            .is_err());
        }
        assert!(VmConfig {
            forwarded_ports: vec![3000, 5173],
            ..Default::default()
        }
        .validate()
        .is_ok());
    }
    #[test]
    fn shell_argument_is_literal() {
        assert_eq!(quote("a'b$(x)"), "'a'\\''b$(x)'");
    }
    #[test]
    fn shared_directory_does_not_accept_lume_suffix_injection() {
        assert!(VmConfig {
            shared_directory: "/tmp/a:rw".into(),
            ..Default::default()
        }
        .validate()
        .is_err());
    }
}
