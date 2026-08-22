mod gateway;
mod settings;

use gateway::GatewayProcesses;
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .manage(GatewayProcesses::default())
        .invoke_handler(tauri::generate_handler![
            settings::load_desktop_settings,
            settings::save_desktop_settings,
            settings::store_credential,
            settings::delete_credential,
            settings::set_dock_icon,
            settings::load_custom_dock_icon,
            gateway::authenticate_connection,
            gateway::ensure_local_gateway,
            gateway::shutdown_gateway,
            gateway::document_engine_status,
            gateway::install_document_engine,
            gateway::remove_document_engine,
        ])
        .build(tauri::generate_context!())
        .expect("failed to build Crab Desktop");

    app.run(|handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            handle.state::<GatewayProcesses>().stop_all();
        }
    });
}
