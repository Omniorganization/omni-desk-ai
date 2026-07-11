#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::fs;

const SERVICE: &str = "ai.omnidesk.desktop";

#[tauri::command]
fn secure_set(key: String, value: String) -> Result<(), String> {
    let entry = keyring::Entry::new(SERVICE, &key).map_err(|error| error.to_string())?;
    entry.set_password(&value).map_err(|error| error.to_string())
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
        if let (Some(drive), Some(path)) = (std::env::var_os("HOMEDRIVE"), std::env::var_os("HOMEPATH")) {
            let mut home = PathBuf::from(drive);
            home.push(path);
            return Ok(home);
        }
    }
    Err("home directory unavailable".to_string())
}

fn safe_workspace(workspace: &str) -> Result<PathBuf, String> {
    let path = Path::new(workspace).canonicalize().map_err(|error| error.to_string())?;
    let home = home_directory()?;
    let approved = home.join("OmniDesktopWorkspace");
    if !path.starts_with(&approved) {
        return Err("workspace must be inside ~/OmniDesktopWorkspace".to_string());
    }
    Ok(path)
}

fn safe_workspace_path(workspace: &str, relative_path: &str) -> Result<PathBuf, String> {
    let root = safe_workspace(workspace)?;
    let relative = Path::new(relative_path);
    if relative.is_absolute()
        || relative.components().any(|part| matches!(part, std::path::Component::ParentDir))
    {
        return Err("path must be relative and cannot contain ..".to_string());
    }
    let candidate = root.join(relative);
    let resolved = candidate.canonicalize().map_err(|error| error.to_string())?;
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
fn list_workspace_directory(workspace: String, relative_path: String) -> Result<Vec<String>, String> {
    const MAX_ENTRIES: usize = 1000;
    let path = safe_workspace_path(&workspace, &relative_path)?;
    if !path.is_dir() {
        return Err("workspace path is not a directory".to_string());
    }
    let mut entries = fs::read_dir(path)
        .map_err(|error| error.to_string())?
        .take(MAX_ENTRIES + 1)
        .map(|entry| entry.map_err(|error| error.to_string()).map(|value| value.file_name().to_string_lossy().to_string()))
        .collect::<Result<Vec<_>, _>>()?;
    if entries.len() > MAX_ENTRIES {
        return Err("workspace directory exceeds the 1000 entry output limit".to_string());
    }
    entries.sort();
    Ok(entries)
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![secure_get, secure_set, read_workspace_file, list_workspace_directory])
        .run(tauri::generate_context!())
        .expect("error while running Omni Desktop App");
}
