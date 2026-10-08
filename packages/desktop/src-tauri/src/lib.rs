mod computer_use;
mod gateway;
mod paths;
mod settings;
mod virtual_machine;

pub use virtual_machine::run_guest_if_requested;

use gateway::GatewayProcesses;
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .manage(GatewayProcesses::default())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            computer_use::computer_use_capabilities,
            computer_use::computer_use_execute,
            computer_use::computer_use_release_ax,
            computer_use::computer_use_open_input_settings,
            virtual_machine::computer_use_vm_list,
            virtual_machine::computer_use_vm_manage,
            virtual_machine::computer_use_vm_capabilities,
            virtual_machine::computer_use_vm_execute,
            virtual_machine::computer_use_vm_release,
            virtual_machine::installer::lume_install_status,
            virtual_machine::installer::install_lume,
            settings::load_desktop_settings,
            settings::save_desktop_settings,
            settings::save_theme_export,
            settings::save_prompt_export,
            settings::store_credential,
            settings::delete_credential,
            settings::set_dock_icon,
            settings::load_custom_dock_icon,
            gateway::authenticate_connection,
            gateway::ensure_local_gateway,
            gateway::installed_gateway_features,
            gateway::install_gateway_suite,
            gateway::install_system_tool,
            gateway::shutdown_gateway,
            gateway::document_engine_status,
            gateway::install_document_engine,
            gateway::remove_document_engine,
        ])
        .build(tauri::generate_context!())
        .expect("failed to build Crab Desktop");

    app.run(|handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            virtual_machine::stop_forwards();
            handle.state::<GatewayProcesses>().stop_all();
        }
    });
}
