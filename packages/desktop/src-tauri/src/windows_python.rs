//! Find Windows Python installations even when Explorer's PATH only contains
//! the Microsoft Store aliases. Discovery never installs or modifies Python;
//! gateway probes still check each candidate's version and ability to run.

use std::path::{Path, PathBuf};
use winreg::enums::{
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_32KEY, KEY_WOW64_64KEY,
};
use winreg::RegKey;

pub(super) fn extend_candidates(candidates: &mut Vec<String>) {
    for path in registry_candidates()
        .into_iter()
        .chain(filesystem_candidates())
    {
        let value = path.to_string_lossy().into_owned();
        if !candidates
            .iter()
            .any(|existing| existing.eq_ignore_ascii_case(&value))
        {
            candidates.push(value);
        }
    }
}

fn registered_executable(
    company: &str,
    executable: Option<String>,
    arguments: Option<String>,
    install_dir: Option<String>,
) -> Option<PathBuf> {
    // The gateway accepts executable paths, not launcher command lines. Do not
    // accidentally execute a registered wrapper without its required arguments.
    if arguments.is_some_and(|value| !value.trim().is_empty()) {
        return None;
    }
    let path = executable
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            // PEP 514 only defines this legacy fallback for PythonCore.
            company
                .eq_ignore_ascii_case("PythonCore")
                .then(|| install_dir.map(|value| PathBuf::from(value).join("python.exe")))
                .flatten()
        })?;
    (path.is_absolute() && path.is_file()).then_some(path)
}

fn registry_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    // Per-user registrations first, then both machine registry views. Enumerate
    // every vendor (including Astral/uv), not just PythonCore.
    for (hive, view) in [
        (HKEY_CURRENT_USER, 0),
        (HKEY_LOCAL_MACHINE, KEY_WOW64_64KEY),
        (HKEY_LOCAL_MACHINE, KEY_WOW64_32KEY),
    ] {
        let Ok(root) =
            RegKey::predef(hive).open_subkey_with_flags("Software\\Python", KEY_READ | view)
        else {
            continue;
        };
        let mut companies: Vec<_> = root.enum_keys().filter_map(Result::ok).collect();
        companies.sort();
        for company in companies {
            if company.eq_ignore_ascii_case("PyLauncher") {
                continue;
            }
            let Ok(company_key) = root.open_subkey_with_flags(&company, KEY_READ | view) else {
                continue;
            };
            let mut tags: Vec<_> = company_key.enum_keys().filter_map(Result::ok).collect();
            tags.sort_by_key(|tag| std::cmp::Reverse(version_key(tag)));
            for tag in tags {
                let Ok(install) = company_key
                    .open_subkey_with_flags(format!("{tag}\\InstallPath"), KEY_READ | view)
                else {
                    continue;
                };
                if let Some(path) = registered_executable(
                    &company,
                    install.get_value("ExecutablePath").ok(),
                    install.get_value("ExecutableArguments").ok(),
                    install.get_value("").ok(),
                ) {
                    push_existing(&mut candidates, path);
                }
            }
        }
    }
    candidates
}

#[derive(Default)]
struct Locations {
    home: Option<PathBuf>,
    local: Option<PathBuf>,
    roaming: Option<PathBuf>,
    program_data: Option<PathBuf>,
    program_files: Vec<PathBuf>,
    system_drive: Option<PathBuf>,
    uv_install_dir: Option<PathBuf>,
}

fn filesystem_candidates() -> Vec<PathBuf> {
    let absolute_env = |name| {
        std::env::var_os(name)
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
    };
    filesystem_candidates_at(&Locations {
        home: dirs::home_dir(),
        local: absolute_env("LOCALAPPDATA").or_else(dirs::data_local_dir),
        roaming: absolute_env("APPDATA").or_else(dirs::config_dir),
        program_data: absolute_env("PROGRAMDATA"),
        program_files: ["ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"]
            .into_iter()
            .filter_map(absolute_env)
            .collect(),
        system_drive: std::env::var_os("SystemDrive")
            .map(|drive| PathBuf::from(drive).join("\\"))
            .filter(|path| path.is_absolute()),
        uv_install_dir: absolute_env("UV_PYTHON_INSTALL_DIR"),
    })
}

fn push_existing(candidates: &mut Vec<PathBuf>, path: PathBuf) {
    // Path joins may retain '/' in a relative suffix. Normalize separators for
    // Windows process launching and deduplication against registry paths.
    let path: PathBuf = path.components().collect();
    if path.is_absolute() && path.is_file() && !candidates.contains(&path) {
        candidates.push(path);
    }
}

fn version_key(name: &str) -> Vec<u32> {
    name.split(|character: char| !character.is_ascii_digit())
        .filter_map(|part| part.parse().ok())
        .collect()
}

fn extend_installations(candidates: &mut Vec<PathBuf>, root: &Path, prefix: &str) {
    let Ok(entries) = std::fs::read_dir(root) else {
        return;
    };
    let mut directories: Vec<_> = entries
        .filter_map(Result::ok)
        .filter(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .to_ascii_lowercase()
                .starts_with(prefix)
        })
        .map(|entry| entry.path())
        .collect();
    // Prefer 3.13.16 over 3.13.6. Only inspect immediate installation folders;
    // never recursively scan a user's drive or project virtual environments.
    directories.sort_by_cached_key(|path| {
        (
            std::cmp::Reverse(version_key(
                &path.file_name().unwrap_or_default().to_string_lossy(),
            )),
            path.clone(),
        )
    });
    for directory in directories {
        push_existing(candidates, directory.join("python.exe"));
    }
}

fn filesystem_candidates_at(locations: &Locations) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(root) = &locations.uv_install_dir {
        extend_installations(&mut candidates, root, "");
    }
    if let Some(root) = &locations.roaming {
        for relative in ["uv/python", "uv/data/python"] {
            extend_installations(&mut candidates, &root.join(relative), "");
        }
    }
    if let Some(root) = &locations.local {
        for relative in ["Programs/Python", "Python", "uv/python"] {
            extend_installations(&mut candidates, &root.join(relative), "");
        }
    }
    for root in locations
        .program_files
        .iter()
        .chain(locations.system_drive.iter())
    {
        extend_installations(&mut candidates, root, "python");
    }
    for root in locations.home.iter().chain(locations.program_data.iter()) {
        for distribution in ["anaconda3", "miniconda3", "miniforge3", "mambaforge"] {
            push_existing(&mut candidates, root.join(distribution).join("python.exe"));
        }
    }
    candidates
}

#[cfg(test)]
mod tests {
    use super::*;

    fn executable(root: &Path, relative: &str) -> PathBuf {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, b"fixture").unwrap();
        path
    }

    #[test]
    fn finds_uv_versions_without_path_and_orders_patch_versions_numerically() {
        let root = tempfile::tempdir().unwrap();
        let old = executable(
            root.path(),
            "uv/python/cpython-3.13.6-windows-x86_64-none/python.exe",
        );
        let new = executable(
            root.path(),
            "uv/python/cpython-3.13.16-windows-x86_64-none/python.exe",
        );
        std::fs::create_dir_all(
            root.path()
                .join("uv/python/cpython-3.13-windows-x86_64-none"),
        )
        .unwrap();
        let candidates = filesystem_candidates_at(&Locations {
            roaming: Some(root.path().into()),
            ..Default::default()
        });
        assert_eq!(candidates, [new, old]);
    }

    #[test]
    fn honors_custom_uv_directory_and_deduplicates_shared_locations() {
        let root = tempfile::tempdir().unwrap();
        let python = executable(
            root.path(),
            "uv/python/cpython-3.12.8-windows-x86_64-none/python.exe",
        );
        let candidates = filesystem_candidates_at(&Locations {
            roaming: Some(root.path().into()),
            local: Some(root.path().into()),
            uv_install_dir: Some(root.path().join("uv/python")),
            ..Default::default()
        });
        assert_eq!(candidates, [python]);
        assert!(!candidates[0].to_string_lossy().contains('/'));
    }

    #[test]
    fn finds_standard_installations_and_conda_with_unicode_and_spaces() {
        let root = tempfile::tempdir().unwrap();
        let expected = [
            executable(
                root.path(),
                "用户/本地/Programs/Python/Python313/python.exe",
            ),
            executable(
                root.path(),
                "用户/本地/Python/pythoncore-3.14-64/python.exe",
            ),
            executable(root.path(), "Program Files/Python312/python.exe"),
            executable(root.path(), "drive/Python311/python.exe"),
            executable(root.path(), "用户/miniconda3/python.exe"),
            executable(root.path(), "ProgramData/miniforge3/python.exe"),
        ];
        executable(root.path(), "Program Files/Unrelated/python.exe");
        executable(root.path(), "drive/project/.venv/python.exe");
        let candidates = filesystem_candidates_at(&Locations {
            home: Some(root.path().join("用户")),
            local: Some(root.path().join("用户/本地")),
            program_files: vec![root.path().join("Program Files")],
            system_drive: Some(root.path().join("drive")),
            program_data: Some(root.path().join("ProgramData")),
            ..Default::default()
        });
        assert_eq!(candidates, expected);
    }

    #[test]
    fn registry_supports_other_vendors_and_pythoncore_legacy_install_path() {
        let root = tempfile::tempdir().unwrap();
        let python = executable(root.path(), "用户 Python/python.exe");
        let path = python.to_string_lossy().into_owned();
        let directory = python.parent().unwrap().to_string_lossy().into_owned();
        assert_eq!(
            registered_executable("Astral", Some(path), None, None),
            Some(python.clone())
        );
        assert_eq!(
            registered_executable("PythonCore", None, None, Some(directory.clone())),
            Some(python)
        );
        assert!(registered_executable("OtherVendor", None, None, Some(directory)).is_none());
    }

    #[test]
    fn registry_ignores_missing_relative_and_argument_dependent_executables() {
        let root = tempfile::tempdir().unwrap();
        let python = executable(root.path(), "python.exe")
            .to_string_lossy()
            .into_owned();
        assert!(
            registered_executable("Astral", Some(python), Some("--special-mode".into()), None)
                .is_none()
        );
        assert!(
            registered_executable("PythonCore", Some("python.exe".into()), None, None).is_none()
        );
        assert!(registered_executable(
            "PythonCore",
            Some(
                root.path()
                    .join("missing.exe")
                    .to_string_lossy()
                    .into_owned()
            ),
            None,
            None
        )
        .is_none());
        assert!(filesystem_candidates_at(&Locations::default()).is_empty());
    }
}
