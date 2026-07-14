#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::path::{Component, Path, PathBuf};

const SERVICE: &str = "ai.omnidesk.desktop";

#[tauri::command]
fn secure_set(key: String, value: String) -> Result<(), String> {
    let entry = keyring::Entry::new(SERVICE, &key).map_err(|error| error.to_string())?;
    entry
        .set_password(&value)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn secure_get(key: String) -> Result<String, String> {
    let entry = keyring::Entry::new(SERVICE, &key).map_err(|error| error.to_string())?;
    match entry.get_password() {
        Ok(value) => Ok(value),
        Err(keyring::Error::NoEntry) => Ok(String::new()),
        Err(error) => Err(error.to_string()),
    }
}

fn home_directory() -> Result<PathBuf, String> {
    if let Some(home) = std::env::var_os("HOME") {
        return Ok(PathBuf::from(home));
    }
    #[cfg(windows)]
    {
        if let Some(profile) = std::env::var_os("USERPROFILE") {
            return Ok(PathBuf::from(profile));
        }
        if let (Some(drive), Some(path)) = (
            std::env::var_os("HOMEDRIVE"),
            std::env::var_os("HOMEPATH"),
        ) {
            let mut home = PathBuf::from(drive);
            home.push(path);
            return Ok(home);
        }
    }
    Err("home directory unavailable".to_string())
}

fn validate_relative_path(relative_path: &str) -> Result<(), String> {
    if relative_path.is_empty() || relative_path.len() > 1024 {
        return Err("path must contain 1 to 1024 characters".to_string());
    }
    if relative_path.chars().any(char::is_control) {
        return Err("path cannot contain control characters".to_string());
    }
    let relative = Path::new(relative_path);
    if relative.is_absolute()
        || relative.components().any(|part| {
            matches!(
                part,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err("path must be relative and cannot contain parent/root components".to_string());
    }
    Ok(())
}

fn approved_workspace_root() -> Result<PathBuf, String> {
    let home = home_directory()?
        .canonicalize()
        .map_err(|_| "home directory is unavailable".to_string())?;
    let declared = home.join("OmniDesktopWorkspace");
    let metadata = fs::symlink_metadata(&declared)
        .map_err(|_| "approved workspace root ~/OmniDesktopWorkspace is unavailable".to_string())?;
    if metadata.file_type().is_symlink() {
        return Err("approved workspace root cannot be a symlink".to_string());
    }
    if !metadata.is_dir() {
        return Err("approved workspace root must be a directory".to_string());
    }
    let approved = declared
        .canonicalize()
        .map_err(|_| "approved workspace root ~/OmniDesktopWorkspace is unavailable".to_string())?;
    if !approved.starts_with(&home) {
        return Err("approved workspace root must remain inside the home directory".to_string());
    }
    Ok(approved)
}

fn safe_workspace(workspace: &str) -> Result<PathBuf, String> {
    let requested = Path::new(workspace);
    if !requested.is_absolute() {
        return Err("workspace path must be absolute".to_string());
    }
    let metadata = fs::symlink_metadata(requested).map_err(|error| error.to_string())?;
    if metadata.file_type().is_symlink() {
        return Err("workspace root cannot be a symlink".to_string());
    }
    if !metadata.is_dir() {
        return Err("workspace root must be a directory".to_string());
    }
    let path = requested
        .canonicalize()
        .map_err(|error| error.to_string())?;
    let approved = approved_workspace_root()?;
    if !path.starts_with(&approved) {
        return Err("workspace must be inside ~/OmniDesktopWorkspace".to_string());
    }
    Ok(path)
}

fn reject_symlink_components(root: &Path, relative: &Path) -> Result<(), String> {
    let mut current = root.to_path_buf();
    for component in relative.components() {
        match component {
            Component::CurDir => continue,
            Component::Normal(segment) => current.push(segment),
            _ => return Err("invalid workspace path component".to_string()),
        }
        if current.exists()
            && fs::symlink_metadata(&current)
                .map_err(|error| error.to_string())?
                .file_type()
                .is_symlink()
        {
            return Err("workspace path cannot traverse symlinks".to_string());
        }
    }
    Ok(())
}

fn safe_workspace_path(workspace: &str, relative_path: &str) -> Result<PathBuf, String> {
    let root = safe_workspace(workspace)?;
    validate_relative_path(relative_path)?;
    let relative = Path::new(relative_path);
    reject_symlink_components(&root, relative)?;
    let resolved = root
        .join(relative)
        .canonicalize()
        .map_err(|error| error.to_string())?;
    if !resolved.starts_with(&root) {
        return Err("path escapes the approved workspace".to_string());
    }
    Ok(resolved)
}

#[tauri::command]
fn read_workspace_file(workspace: String, relative_path: String) -> Result<String, String> {
    const MAX_BYTES: u64 = 1024 * 1024;
    let path = safe_workspace_path(&workspace, &relative_path)?;
    let metadata = fs::symlink_metadata(&path).map_err(|error| error.to_string())?;
    if !metadata.file_type().is_file() || metadata.len() > MAX_BYTES {
        return Err("workspace file must be regular and no larger than 1 MiB".to_string());
    }
    fs::read_to_string(path).map_err(|error| error.to_string())
}

#[tauri::command]
fn list_workspace_directory(
    workspace: String,
    relative_path: String,
) -> Result<Vec<String>, String> {
    const MAX_ENTRIES: usize = 1000;
    let path = safe_workspace_path(&workspace, &relative_path)?;
    if !path.is_dir() {
        return Err("workspace path is not a directory".to_string());
    }
    let mut entries = fs::read_dir(path)
        .map_err(|error| error.to_string())?
        .take(MAX_ENTRIES + 1)
        .map(|entry| {
            entry
                .map_err(|error| error.to_string())
                .map(|value| value.file_name().to_string_lossy().to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    if entries.len() > MAX_ENTRIES {
        return Err("workspace directory exceeds the 1000 entry output limit".to_string());
    }
    entries.sort();
    Ok(entries)
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            secure_get,
            secure_set,
            read_workspace_file,
            list_workspace_directory
        ])
        .run(tauri::generate_context!())
        .expect("error while running Omni Desktop App");
}

#[cfg(test)]
mod tests {
    use super::validate_relative_path;

    #[test]
    fn accepts_bounded_relative_paths() {
        assert!(validate_relative_path(".").is_ok());
        assert!(validate_relative_path("project/src/main.rs").is_ok());
    }

    #[test]
    fn rejects_escape_and_absolute_paths() {
        assert!(validate_relative_path("../secret").is_err());
        assert!(validate_relative_path("project/../../secret").is_err());
        assert!(validate_relative_path("/etc/passwd").is_err());
    }

    #[test]
    fn rejects_control_and_oversized_paths() {
        assert!(validate_relative_path("bad\0path").is_err());
        assert!(validate_relative_path(&"a".repeat(1025)).is_err());
    }
}
