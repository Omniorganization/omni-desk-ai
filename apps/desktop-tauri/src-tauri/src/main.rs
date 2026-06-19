#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::Command;

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

fn allowed_command(command: &str) -> bool {
    matches!(command, "echo" | "pwd" | "ls" | "cat")
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

#[tauri::command]
fn run_workspace_command(workspace: String, command: String, args: Vec<String>) -> Result<String, String> {
    if !allowed_command(&command) {
        return Err("command is not in the signed allowlist".to_string());
    }
    let cwd = safe_workspace(&workspace)?;
    let output = Command::new(command).args(args).current_dir(cwd).output().map_err(|error| error.to_string())?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    if !output.status.success() {
        return Err(format!("command failed: {stderr}"));
    }
    Ok(stdout)
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![secure_get, secure_set, run_workspace_command])
        .run(tauri::generate_context!())
        .expect("error while running Omni Desktop App");
}
