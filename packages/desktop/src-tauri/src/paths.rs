use std::path::{Path, PathBuf};

/// Match crabcode_core.paths so Desktop and its Gateway use the same home.
pub(crate) fn config_home() -> Result<PathBuf, String> {
    let value = match std::env::var("CRABCODE_HOME") {
        Ok(value) => Some(value),
        Err(std::env::VarError::NotPresent) => None,
        Err(error) => return Err(format!("Unable to read CRABCODE_HOME: {error}")),
    };
    resolve_config_home(value.as_deref(), dirs::home_dir().as_deref())
}

fn resolve_config_home(value: Option<&str>, home: Option<&Path>) -> Result<PathBuf, String> {
    let user_home = || home.ok_or_else(|| "Unable to locate the user home directory".to_string());
    let value = value.unwrap_or_default().trim();
    if value.is_empty() {
        return Ok(user_home()?.join(".crabcode"));
    }
    let configured = Path::new(value);
    let path = if let Ok(rest) = configured.strip_prefix("~") {
        user_home()?.join(rest)
    } else {
        configured.to_path_buf()
    };
    if !path.is_absolute() {
        return Err("CRABCODE_HOME must be an absolute directory path (or start with ~/)".into());
    }
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn home() -> PathBuf {
        #[cfg(windows)]
        return PathBuf::from(r"C:\Users\test");
        #[cfg(not(windows))]
        return PathBuf::from("/home/test");
    }

    #[test]
    fn missing_or_blank_override_uses_default() {
        let home = home();
        for value in [None, Some(""), Some(" \t ")] {
            assert_eq!(
                resolve_config_home(value, Some(&home)).unwrap(),
                home.join(".crabcode")
            );
        }
    }

    #[test]
    fn explicit_directory_does_not_require_a_user_home() {
        let directory = home().join("custom settings");
        assert_eq!(
            resolve_config_home(directory.to_str(), None).unwrap(),
            directory
        );
    }

    #[test]
    fn tilde_expands_to_current_user_home() {
        let home = home();
        assert_eq!(
            resolve_config_home(Some("~/custom settings"), Some(&home)).unwrap(),
            home.join("custom settings")
        );
        assert_eq!(resolve_config_home(Some("~"), Some(&home)).unwrap(), home);
    }

    #[test]
    fn relative_directory_is_rejected() {
        for value in ["relative", "./config", "../config"] {
            assert!(resolve_config_home(Some(value), Some(&home()))
                .unwrap_err()
                .contains("absolute"));
        }
    }
}
