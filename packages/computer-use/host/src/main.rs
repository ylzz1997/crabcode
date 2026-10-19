#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let result = (|| {
        if args.iter().any(|arg| arg == "--version") {
            println!("{}", env!("CARGO_PKG_VERSION"));
            return Ok(());
        }
        if args.iter().any(|arg| arg == "--probe-capabilities") {
            // Do not start an executor, install an app, or open permission
            // prompts. A fresh process avoids macOS's cached TCC answers.
            println!("{}", serde_json::to_string(
                &crabcode_computer_use_core::detect_capabilities()
            ).map_err(|error| error.to_string())?);
            return Ok(());
        }
        let dir = args
            .windows(2)
            .find(|pair| pair[0] == "--state-dir")
            .map(|pair| std::path::PathBuf::from(&pair[1]))
            .map(Ok)
            .unwrap_or_else(crabcode_computer_use_host::state_dir)?;
        if args.iter().any(|arg| arg == "--install-and-start") {
            let source = std::env::current_exe().map_err(|e| e.to_string())?;
            let executable = crabcode_computer_use_host::install(&source, &dir)?;
            crabcode_computer_use_host::ensure_running(&dir, &executable)?;
            // Do not print the endpoint token to an output channel or log.
            println!("Computer Use ready");
            return Ok(());
        }
        crabcode_computer_use_host::serve(dir)
    })();
    if let Err(error) = result {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
