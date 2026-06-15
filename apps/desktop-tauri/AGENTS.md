# Desktop Tauri Rules

- Preserve per-install device identity and signed sensitive requests.
- Keep operator tokens and device private keys in the OS secure store, never localStorage.
- Do not add unsigned native commands or shell execution without owner approval, audit logging, and tests.
- Release builds must use locked Rust and npm dependencies.
