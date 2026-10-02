use super::write_identity_once;
use std::cell::Cell;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

static TEST_LOCK_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn test_lock_path() -> PathBuf {
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!(
        "omnidesk-identity-test-{}-{nonce}-{}.lock",
        std::process::id(),
        TEST_LOCK_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ))
}

#[test]
fn rejects_identity_replacement_and_read_errors_without_writes() {
    let path = test_lock_path();
    let writes = Cell::new(0);
    let write = || {
        writes.set(writes.get() + 1);
        Ok(())
    };
    assert_eq!(
        write_identity_once(&path, "new", || Ok("existing".into()), write),
        Err("desktop identity already exists; refusing credential replacement".into())
    );
    assert_eq!(
        write_identity_once(&path, "new", || Err("store denied".into()), write),
        Err("store denied".into())
    );
    assert_eq!(
        write_identity_once(&path, "existing", || Ok("existing".into()), write),
        Ok(())
    );
    assert_eq!(writes.get(), 0);
    assert_eq!(
        write_identity_once(&path, "new", || Ok(String::new()), write),
        Ok(())
    );
    assert_eq!(writes.get(), 1);
    std::fs::remove_file(path).unwrap();
}

#[test]
fn identity_lock_child() {
    let Some(path) = std::env::var_os("OMNIDESK_TEST_IDENTITY_LOCK") else {
        return;
    };
    let result = write_identity_once(
        PathBuf::from(path).as_path(),
        "new",
        || Ok(String::new()),
        || Ok(()),
    );
    assert_eq!(
        result.is_err(),
        std::env::var_os("OMNIDESK_TEST_EXPECT_BUSY").is_some()
    );
}

#[test]
fn identity_lock_excludes_another_process_and_releases_on_drop() {
    let path = test_lock_path();
    let file = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&path)
        .unwrap();
    fs2::FileExt::try_lock_exclusive(&file).unwrap();
    let child = |busy: bool| {
        let mut command = std::process::Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "identity_tests::identity_lock_child",
                "--nocapture",
            ])
            .env("OMNIDESK_TEST_IDENTITY_LOCK", &path)
            .env_remove("OMNIDESK_TEST_EXPECT_BUSY");
        if busy {
            command.env("OMNIDESK_TEST_EXPECT_BUSY", "1");
        }
        assert!(command.status().unwrap().success());
    };
    child(true);
    drop(file);
    child(false);
    std::fs::remove_file(path).unwrap();
}
