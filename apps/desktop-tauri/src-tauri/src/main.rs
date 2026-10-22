#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::path::{Component, Path, PathBuf};
use tauri::Manager;

mod device_keys;

const SERVICE: &str = "ai.omnidesk.desktop";
const DEVICE_ID_KEY: &str = "omni.deviceId.v2";
const DEVICE_PUBLIC_KEY_KEY: &str = "omni.devicePublicKeyPem.v2";
const DEVICE_PRIVATE_KEY_KEY: &str = "omni.devicePrivateKeyJwk.v2";
const SESSION_KEYS: [&str; 3] = ["omni.gateway", "omni.operatorToken", "omni.actor"];

/// Keychain entries the webview may read. The device private key is never
/// returned: identity creation and signing happen in Rust.
fn readable_keychain_key(key: &str) -> Result<&str, String> {
    if SESSION_KEYS.contains(&key) || key == DEVICE_ID_KEY || key == DEVICE_PUBLIC_KEY_KEY {
        Ok(key)
    } else {
        Err("unsupported keychain key".to_string())
    }
}

/// Keychain entries the webview may write: session settings only.
fn writable_keychain_key(key: &str) -> Result<&str, String> {
    if SESSION_KEYS.contains(&key) {
        Ok(key)
    } else {
        Err("unsupported keychain key".to_string())
    }
}

fn keychain_read(key: &str) -> Result<String, String> {
    let entry = keyring::Entry::new(SERVICE, key).map_err(|error| error.to_string())?;
    match entry.get_password() {
        Ok(value) => Ok(value),
        Err(keyring::Error::NoEntry) => Ok(String::new()),
        Err(error) => Err(error.to_string()),
    }
}

fn keychain_write(key: &str, value: &str) -> Result<(), String> {
    keyring::Entry::new(SERVICE, key)
        .map_err(|error| error.to_string())?
        .set_password(value)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn secure_set(key: String, value: String) -> Result<(), String> {
    keychain_write(writable_keychain_key(&key)?, &value)
}

#[derive(Debug, PartialEq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopIdentityView {
    device_id: String,
    public_key_pem: String,
}

fn load_or_create_identity(
    lock_path: &Path,
    read: impl Fn(&str) -> Result<String, String>,
    write: impl Fn(&str, &str) -> Result<(), String>,
) -> Result<DesktopIdentityView, String> {
    with_identity_lock(lock_path, || {
        let device_id = read(DEVICE_ID_KEY)?;
        let public_key_pem = read(DEVICE_PUBLIC_KEY_KEY)?;
        let private_present = !read(DEVICE_PRIVATE_KEY_KEY)?.is_empty();
        if !device_id.is_empty() && !public_key_pem.is_empty() && private_present {
            return Ok(DesktopIdentityView {
                device_id,
                public_key_pem,
            });
        }
        if !device_id.is_empty() || !public_key_pem.is_empty() || private_present {
            return Err(
                "desktop device identity is incomplete; secure-store recovery is required"
                    .to_string(),
            );
        }
        let generated = device_keys::generate_identity()?;
        write(DEVICE_ID_KEY, &generated.device_id)?;
        write(DEVICE_PUBLIC_KEY_KEY, &generated.public_key_pem)?;
        write(DEVICE_PRIVATE_KEY_KEY, &generated.private_jwk)?;
        Ok(DesktopIdentityView {
            device_id: generated.device_id,
            public_key_pem: generated.public_key_pem,
        })
    })
}

#[tauri::command]
fn device_identity(app: tauri::AppHandle) -> Result<DesktopIdentityView, String> {
    let directory = app
        .path()
        .app_local_data_dir()
        .map_err(|error| error.to_string())?;
    fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    load_or_create_identity(
        &directory.join("device-identity.lock"),
        keychain_read,
        keychain_write,
    )
}

#[tauri::command]
fn device_sign_request(
    method: String,
    path: String,
    body_sha256: String,
    timestamp: String,
    nonce: String,
) -> Result<String, String> {
    let message = device_keys::request_message(&method, &path, &body_sha256, &timestamp, &nonce)?;
    let private_jwk = keychain_read(DEVICE_PRIVATE_KEY_KEY)?;
    if private_jwk.is_empty() {
        return Err("desktop device private key is not initialized".to_string());
    }
    device_keys::sign(&private_jwk, &message)
}

struct IdentityStoreLock(fs::File);

impl Drop for IdentityStoreLock {
    fn drop(&mut self) {
        // Closing alone can leave a flock held by a descriptor inherited at fork.
        // Unlock only after the guarded read/write, including all early returns.
        let _ = fs2::FileExt::unlock(&self.0);
    }
}

fn with_identity_lock<T>(
    lock_path: &Path,
    guarded: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(lock_path)
        .map_err(|error| error.to_string())?;
    fs2::FileExt::try_lock_exclusive(&file)
        .map_err(|_| "desktop identity store is busy; retry initialization".to_string())?;
    let _lock = IdentityStoreLock(file);
    guarded()
}

#[cfg(test)]
fn write_identity_once(
    lock_path: &Path,
    value: &str,
    read: impl FnOnce() -> Result<String, String>,
    write: impl FnOnce() -> Result<(), String>,
) -> Result<(), String> {
    with_identity_lock(lock_path, || {
        let existing = read()?;
        if !existing.is_empty() {
            return if existing == value {
                Ok(())
            } else {
                Err("desktop identity already exists; refusing credential replacement".to_string())
            };
        }
        write()
    })
}

#[tauri::command]
fn secure_get(key: String) -> Result<String, String> {
    keychain_read(readable_keychain_key(&key)?)
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
        if let (Some(drive), Some(path)) =
            (std::env::var_os("HOMEDRIVE"), std::env::var_os("HOMEPATH"))
        {
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
            device_identity,
            device_sign_request,
            read_workspace_file,
            list_workspace_directory
        ])
        .run(tauri::generate_context!())
        .expect("error while running Omni Desktop App");
}

#[cfg(test)]
mod identity_tests;

#[cfg(test)]
mod tests {
    use super::{
        load_or_create_identity, readable_keychain_key, validate_relative_path,
        writable_keychain_key, DEVICE_ID_KEY, DEVICE_PRIVATE_KEY_KEY, DEVICE_PUBLIC_KEY_KEY,
    };
    use std::cell::RefCell;
    use std::collections::HashMap;

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

    #[test]
    fn webview_cannot_read_private_key_or_write_identity() {
        assert!(readable_keychain_key("omni.operatorToken").is_ok());
        assert!(readable_keychain_key(DEVICE_ID_KEY).is_ok());
        assert!(readable_keychain_key(DEVICE_PUBLIC_KEY_KEY).is_ok());
        assert!(readable_keychain_key(DEVICE_PRIVATE_KEY_KEY).is_err());
        assert!(readable_keychain_key("omni.other").is_err());
        assert!(writable_keychain_key("omni.gateway").is_ok());
        for key in [
            DEVICE_ID_KEY,
            DEVICE_PUBLIC_KEY_KEY,
            DEVICE_PRIVATE_KEY_KEY,
            "",
            "omni.other",
        ] {
            assert!(writable_keychain_key(key).is_err(), "{key}");
        }
    }

    fn lock_path(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("omnidesk-{name}-{}.lock", std::process::id()))
    }

    #[test]
    fn identity_is_created_once_and_reused() {
        let store = RefCell::new(HashMap::<String, String>::new());
        let read = |key: &str| Ok(store.borrow().get(key).cloned().unwrap_or_default());
        let write = |key: &str, value: &str| {
            store
                .borrow_mut()
                .insert(key.to_string(), value.to_string());
            Ok(())
        };
        let path = lock_path("identity-create");
        let first = load_or_create_identity(&path, read, write).unwrap();
        assert!(first.device_id.starts_with("desk_"));
        assert!(store.borrow().contains_key(DEVICE_PRIVATE_KEY_KEY));
        let again = load_or_create_identity(&path, read, |_: &str, _: &str| {
            Err("no writes expected".to_string())
        })
        .unwrap();
        assert_eq!(again, first);
    }

    #[test]
    fn incomplete_identity_requires_recovery_instead_of_regeneration() {
        let store = RefCell::new(HashMap::from([(
            DEVICE_ID_KEY.to_string(),
            "desk_x".to_string(),
        )]));
        let read = |key: &str| Ok(store.borrow().get(key).cloned().unwrap_or_default());
        let result =
            load_or_create_identity(&lock_path("identity-partial"), read, |_: &str, _: &str| {
                Err("no writes expected".to_string())
            });
        assert!(result.unwrap_err().contains("incomplete"));
    }
}
