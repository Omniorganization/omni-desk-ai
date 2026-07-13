#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::io::Write;
use std::path::{Component, Path, PathBuf};

const SERVICE: &str = "ai.omnidesk.desktop";
const MAX_FILE_BYTES: usize = 1024 * 1024;

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
        return Err(
            "path must be relative and cannot contain parent/root components".to_string(),
        );
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
    let path = requested.canonicalize().map_err(|error| error.to_string())?;
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

fn safe_workspace_target(workspace: &str, relative_path: &str) -> Result<PathBuf, String> {
    let root = safe_workspace(workspace)?;
    validate_relative_path(relative_path)?;
    let relative = Path::new(relative_path);
    reject_symlink_components(&root, relative)?;
    let target = root.join(relative);
    let parent = target
        .parent()
        .ok_or_else(|| "workspace target has no parent".to_string())?;
    let parent_resolved = parent.canonicalize().map_err(|error| error.to_string())?;
    if !parent_resolved.starts_with(&root) {
        return Err("target parent escapes the approved workspace".to_string());
    }
    if target.exists() {
        let metadata = fs::symlink_metadata(&target).map_err(|error| error.to_string())?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err("workspace target must be a regular file".to_string());
        }
    }
    Ok(target)
}

fn read_bounded(path: &Path) -> Result<String, String> {
    let metadata = fs::symlink_metadata(path).map_err(|error| error.to_string())?;
    if !metadata.file_type().is_file() || metadata.len() > MAX_FILE_BYTES as u64 {
        return Err("workspace file must be regular and no larger than 1 MiB".to_string());
    }
    fs::read_to_string(path).map_err(|error| error.to_string())
}

fn sha256_hex(input: &[u8]) -> String {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
        0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
        0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
        0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
        0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
        0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ];
    let mut h = [
        0x6a09e667u32,
        0xbb67ae85,
        0x3c6ef372,
        0xa54ff53a,
        0x510e527f,
        0x9b05688c,
        0x1f83d9ab,
        0x5be0cd19,
    ];
    let bit_len = (input.len() as u64) * 8;
    let mut data = input.to_vec();
    data.push(0x80);
    while data.len() % 64 != 56 {
        data.push(0);
    }
    data.extend_from_slice(&bit_len.to_be_bytes());
    for chunk in data.chunks_exact(64) {
        let mut w = [0u32; 64];
        for (index, bytes) in chunk.chunks_exact(4).enumerate().take(16) {
            w[index] = u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
        }
        for index in 16..64 {
            let s0 = w[index - 15].rotate_right(7)
                ^ w[index - 15].rotate_right(18)
                ^ (w[index - 15] >> 3);
            let s1 = w[index - 2].rotate_right(17)
                ^ w[index - 2].rotate_right(19)
                ^ (w[index - 2] >> 10);
            w[index] = w[index - 16]
                .wrapping_add(s0)
                .wrapping_add(w[index - 7])
                .wrapping_add(s1);
        }
        let mut a = h[0];
        let mut b = h[1];
        let mut c = h[2];
        let mut d = h[3];
        let mut e = h[4];
        let mut f = h[5];
        let mut g = h[6];
        let mut hh = h[7];
        for index in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[index])
                .wrapping_add(w[index]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }
        for (target, value) in h.iter_mut().zip([a, b, c, d, e, f, g, hh]) {
            *target = target.wrapping_add(value);
        }
    }
    h.iter().map(|value| format!("{value:08x}")).collect()
}

fn verify_expected_sha(current: &str, expected_sha256: &str) -> Result<(), String> {
    let expected = expected_sha256.trim().to_ascii_lowercase();
    if expected.len() != 64 || !expected.chars().all(|ch| ch.is_ascii_hexdigit()) {
        return Err("expected_sha256 must be a 64-character hexadecimal digest".to_string());
    }
    if sha256_hex(current.as_bytes()) != expected {
        return Err("workspace file changed since approval; expected_sha256 mismatch".to_string());
    }
    Ok(())
}

fn atomic_write(path: &Path, content: &str) -> Result<(), String> {
    if content.len() > MAX_FILE_BYTES {
        return Err("workspace write exceeds the 1 MiB limit".to_string());
    }
    let parent = path
        .parent()
        .ok_or_else(|| "workspace target has no parent".to_string())?;
    let file_name = path
        .file_name()
        .ok_or_else(|| "workspace target has no file name".to_string())?
        .to_string_lossy();
    let temp = parent.join(format!(".{file_name}.omnidesk-{}.tmp", std::process::id()));
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temp)
            .map_err(|error| error.to_string())?;
        file.write_all(content.as_bytes())
            .map_err(|error| error.to_string())?;
        file.sync_all().map_err(|error| error.to_string())?;
        fs::rename(&temp, path).map_err(|error| error.to_string())?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

#[tauri::command]
fn read_workspace_file(workspace: String, relative_path: String) -> Result<String, String> {
    let path = safe_workspace_path(&workspace, &relative_path)?;
    read_bounded(&path)
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

#[tauri::command]
fn write_workspace_file(
    workspace: String,
    relative_path: String,
    content: String,
    expected_sha256: Option<String>,
) -> Result<String, String> {
    let path = safe_workspace_target(&workspace, &relative_path)?;
    if path.exists() {
        let current = read_bounded(&path)?;
        let expected = expected_sha256
            .as_deref()
            .ok_or_else(|| "expected_sha256 is required when overwriting a file".to_string())?;
        verify_expected_sha(&current, expected)?;
    } else if expected_sha256.is_some() {
        return Err("expected_sha256 must be omitted when creating a new file".to_string());
    }
    atomic_write(&path, &content)?;
    Ok(sha256_hex(content.as_bytes()))
}

#[tauri::command]
fn patch_workspace_file(
    workspace: String,
    relative_path: String,
    expected_sha256: String,
    find: String,
    replace: String,
) -> Result<String, String> {
    if find.is_empty() {
        return Err("patch find text cannot be empty".to_string());
    }
    let path = safe_workspace_path(&workspace, &relative_path)?;
    let current = read_bounded(&path)?;
    verify_expected_sha(&current, &expected_sha256)?;
    let occurrences = current.match_indices(&find).count();
    if occurrences != 1 {
        return Err("patch find text must occur exactly once".to_string());
    }
    let updated = current.replacen(&find, &replace, 1);
    atomic_write(&path, &updated)?;
    Ok(sha256_hex(updated.as_bytes()))
}

#[tauri::command]
fn diff_workspace_file(
    workspace: String,
    relative_path: String,
    proposed_content: String,
) -> Result<String, String> {
    const MAX_DIFF_LINES: usize = 400;
    let path = safe_workspace_target(&workspace, &relative_path)?;
    let current = if path.exists() {
        read_bounded(&path)?
    } else {
        String::new()
    };
    if proposed_content.len() > MAX_FILE_BYTES {
        return Err("proposed content exceeds the 1 MiB limit".to_string());
    }
    let before: Vec<&str> = current.lines().collect();
    let after: Vec<&str> = proposed_content.lines().collect();
    let mut output = vec!["--- current".to_string(), "+++ proposed".to_string()];
    let max_lines = before.len().max(after.len());
    for index in 0..max_lines {
        let old = before.get(index).copied();
        let new = after.get(index).copied();
        if old == new {
            continue;
        }
        if let Some(value) = old {
            output.push(format!("-{}", value));
        }
        if let Some(value) = new {
            output.push(format!("+{}", value));
        }
        if output.len() >= MAX_DIFF_LINES {
            output.push("... diff truncated at 400 lines".to_string());
            break;
        }
    }
    Ok(output.join("\n"))
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            secure_get,
            secure_set,
            read_workspace_file,
            list_workspace_directory,
            write_workspace_file,
            patch_workspace_file,
            diff_workspace_file
        ])
        .run(tauri::generate_context!())
        .expect("error while running Omni Desktop App");
}

#[cfg(test)]
mod tests {
    use super::{sha256_hex, validate_relative_path};

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
    fn sha256_matches_standard_vector() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
