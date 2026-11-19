//! Owner-only files for secrets: the device token and the tunnel private key in the state file.
//!
//! POSIX: the file is created with mode 0600 and kept there, so it is never readable by others.
//! Windows: mode bits do not exist. Inherited ACL entries are removed, the current account (by SID)
//! gets full control, and the broad groups (Everyone, Authenticated Users, Users) are removed, using the
//! built-in `whoami` and `icacls`; nothing extra is installed. SYSTEM and Administrators may keep access
//! where Windows sets it explicitly: they can read any file anyway, and Windows' OpenSSH accepts the same
//! set for private keys. What this blocks is another ordinary user on the same machine.

use std::io::Write;
use std::path::Path;

/// Create or replace `path` with `data`, restricted to the owner before any secret is written.
pub fn write_private(path: &Path, data: &[u8]) -> std::io::Result<()> {
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(path)?;
    restrict_to_owner(path)?; // the file is empty at this point
    f.write_all(data)?;
    f.sync_all()
}

#[cfg(unix)]
pub fn restrict_to_owner(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    // `mode` above only applies to new files; this also covers a file that already existed.
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
}

#[cfg(windows)]
pub fn restrict_to_owner(path: &Path) -> std::io::Result<()> {
    let sid = windows::current_sid()?;
    // Everyone, Authenticated Users, Users: by SID, so it works on any Windows language.
    windows::run("icacls", &[path.as_os_str(), "/inheritance:r".as_ref(), "/grant:r".as_ref(),
                             format!("*{sid}:F").as_ref(), "/remove:g".as_ref(), "*S-1-1-0".as_ref(),
                             "*S-1-5-11".as_ref(), "*S-1-5-32-545".as_ref()])
        .map(|_| ())
}

#[cfg(windows)]
mod windows {
    use std::ffi::OsStr;
    use std::io::{Error, ErrorKind};
    use std::os::windows::process::CommandExt;
    use std::process::Command;

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    pub fn run(program: &str, args: &[&OsStr]) -> std::io::Result<String> {
        let out = Command::new(program).args(args).creation_flags(CREATE_NO_WINDOW).output()?;
        if !out.status.success() {
            return Err(Error::new(ErrorKind::PermissionDenied,
                                  format!("{program} failed with {}", out.status)));
        }
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    /// The current account's SID, from one CSV row: "DOMAIN\user","S-1-5-...".
    pub fn current_sid() -> std::io::Result<String> {
        let out = run("whoami", &["/user".as_ref(), "/fo".as_ref(), "csv".as_ref(), "/nh".as_ref()])?;
        let sid = out.trim().rsplit(',').next().unwrap_or("").trim().trim_matches('"').to_string();
        if !sid.starts_with("S-") {
            return Err(Error::new(ErrorKind::InvalidData, "could not read the current account SID"));
        }
        Ok(sid)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn owner_only(path: &Path) -> bool {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::metadata(path).unwrap().permissions().mode() & 0o077 == 0
        }
        #[cfg(windows)]
        {
            let out = windows::run("icacls", &[path.as_os_str()]).unwrap().to_lowercase();
            let entries: Vec<&str> = out.lines().filter(|l| l.contains(":(")).collect();
            // Compare the account name only: icacls may show a truncated (NetBIOS) machine or domain name
            // where whoami shows the full one, as on GitHub's Windows runners.
            let who = windows::run("whoami", &[]).unwrap().trim().to_lowercase();
            let user = who.rsplit('\\').next().unwrap_or(&who).to_string();
            // Allowed: the owner, plus SYSTEM and Administrators (see the module comment). Nothing inherited.
            let allowed = |e: &str| e.contains(&format!("\\{user}:(")) || e.contains("nt authority\\system:(")
                || e.contains("builtin\\administrators:(");
            let ok = !entries.is_empty() && entries.iter().all(|e| !e.contains("(i)") && allowed(e))
                && entries.iter().any(|e| e.contains(&format!("\\{user}:(")));
            if !ok {
                eprintln!("icacls for {} (whoami {who}):\n{out}", path.display());
            }
            ok
        }
    }

    #[test]
    fn secrets_are_owner_only_including_on_replace() {
        let dir = std::env::temp_dir().join(format!("idlemesh-secure-fs-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("state.json");
        std::fs::write(&p, b"old, created with default permissions").unwrap();
        #[cfg(windows)] // make sure another group can read it, whatever the folder's defaults are
        windows::run("icacls", &[p.as_os_str(), "/grant".as_ref(), "*S-1-5-32-545:R".as_ref()]).unwrap();
        assert!(!owner_only(&p), "test precondition: a plain file is not owner-only");
        write_private(&p, b"secret").unwrap();
        assert_eq!(std::fs::read(&p).unwrap(), b"secret");
        assert!(owner_only(&p));
        std::fs::remove_dir_all(&dir).ok();
    }
}
