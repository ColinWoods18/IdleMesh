//! Built-in bandwidth test through the worker tunnel (S5.2), so nodes need no iperf3 or other software (D-07).
//!
//! Only the coordinator can open a tunnel connection (its certificate is pinned), and it starts a test by
//! sending `MAGIC` as the first byte after the TLS handshake. llama.cpp RPC connections start with a command
//! byte below 64, so the two never collide; a connection that does not start with `MAGIC` goes to the worker
//! unchanged. The test never touches the worker.
//!
//! Protocol, all integers little-endian:
//!   client: MAGIC, VERSION               agent: MAGIC, VERSION   (an older agent forwards MAGIC to the
//!                                                                 worker, which closes the connection)
//!   then frames until the client sends DONE or closes:
//!   client: UPLOAD, u64 n, n bytes       agent: u64 bytes received, u64 microseconds from first to last byte
//!   client: DOWNLOAD, u64 n              agent: n bytes (the client times them as they arrive)
//!   client: DONE
//! Each side times what it receives, so the figure leaves out the round trip that starts a transfer.

use std::time::{Duration, Instant};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const MAGIC: u8 = 0xB7;
// llama.cpp v0.9.0 RPC commands are 0..=21; anything below 64 is left to the worker.
const _: () = assert!(MAGIC >= 64);
pub const VERSION: u8 = 1;
const UPLOAD: u8 = 1;
const DOWNLOAD: u8 = 2;
const DONE: u8 = 0;
/// Largest single transfer, and the most one test may move in both directions together.
pub const MAX_TRANSFER: u64 = 64 << 20;
pub const MAX_SESSION: u64 = 256 << 20;
/// A whole test must finish in this time; a slow or stalled peer cannot hold the agent's tunnel thread.
pub const SESSION_TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Debug, Default, PartialEq)]
pub struct Moved {
    pub received: u64,
    pub sent: u64,
}

/// Serve one bandwidth test on a connection whose `MAGIC` byte has already been read.
pub async fn serve<S: AsyncRead + AsyncWrite + Unpin>(s: &mut S) -> Result<Moved, String> {
    match tokio::time::timeout(SESSION_TIMEOUT, serve_frames(s)).await {
        Ok(r) => r,
        Err(_) => Err(format!("bandwidth test did not finish in {} s", SESSION_TIMEOUT.as_secs())),
    }
}

async fn serve_frames<S: AsyncRead + AsyncWrite + Unpin>(s: &mut S) -> Result<Moved, String> {
    let io = |e: std::io::Error| e.to_string();
    let version = s.read_u8().await.map_err(io)?;
    if version != VERSION {
        return Err(format!("bandwidth test version {version} is not supported (this agent speaks {VERSION})"));
    }
    s.write_all(&[MAGIC, VERSION]).await.map_err(io)?;
    s.flush().await.map_err(io)?;
    let mut moved = Moved::default();
    let mut buf = vec![0u8; 1 << 16];
    loop {
        let op = match s.read_u8().await {
            Ok(op) => op,
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(moved),
            Err(e) => return Err(e.to_string()),
        };
        if op == DONE {
            return Ok(moved);
        }
        let n = s.read_u64_le().await.map_err(io)?;
        if n == 0 || n > MAX_TRANSFER || moved.received + moved.sent + n > MAX_SESSION {
            return Err(format!("bandwidth test asked to move {n} bytes; the limit is {MAX_TRANSFER} per transfer \
                                and {MAX_SESSION} per test"));
        }
        match op {
            UPLOAD => {
                let mut left = n;
                let mut first: Option<Instant> = None;
                while left > 0 {
                    let want = left.min(buf.len() as u64) as usize;
                    let got = s.read(&mut buf[..want]).await.map_err(io)?;
                    if got == 0 {
                        return Err(format!("connection closed with {left} of {n} bytes still to come"));
                    }
                    first.get_or_insert_with(Instant::now);
                    left -= got as u64;
                }
                let micros = first.map_or(0, |t| t.elapsed().as_micros() as u64);
                moved.received += n;
                s.write_u64_le(n).await.map_err(io)?;
                s.write_u64_le(micros).await.map_err(io)?;
                s.flush().await.map_err(io)?;
            }
            DOWNLOAD => {
                buf.fill(0);
                let mut left = n;
                while left > 0 {
                    let chunk = left.min(buf.len() as u64) as usize;
                    s.write_all(&buf[..chunk]).await.map_err(io)?;
                    left -= chunk as u64;
                }
                s.flush().await.map_err(io)?;
                moved.sent += n;
            }
            other => return Err(format!("unknown bandwidth test frame {other}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rt() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap()
    }

    #[test]
    fn upload_and_download_round_trip() {
        rt().block_on(async {
            let (mut client, mut agent) = tokio::io::duplex(1 << 16);
            let server = tokio::spawn(async move { serve(&mut agent).await });
            client.write_all(&[VERSION]).await.unwrap();
            let mut hello = [0u8; 2];
            client.read_exact(&mut hello).await.unwrap();
            assert_eq!(hello, [MAGIC, VERSION]);

            let n = 300_000u64;
            client.write_u8(UPLOAD).await.unwrap();
            client.write_u64_le(n).await.unwrap();
            client.write_all(&vec![7u8; n as usize]).await.unwrap();
            assert_eq!(client.read_u64_le().await.unwrap(), n);
            let _micros = client.read_u64_le().await.unwrap();

            client.write_u8(DOWNLOAD).await.unwrap();
            client.write_u64_le(n).await.unwrap();
            let mut got = vec![0u8; n as usize];
            client.read_exact(&mut got).await.unwrap();

            client.write_u8(DONE).await.unwrap();
            assert_eq!(server.await.unwrap(), Ok(Moved { received: n, sent: n }));
        });
    }

    #[test]
    fn oversized_and_unknown_requests_are_refused() {
        rt().block_on(async {
            let (mut client, mut agent) = tokio::io::duplex(1 << 16);
            let server = tokio::spawn(async move { serve(&mut agent).await });
            client.write_all(&[VERSION, UPLOAD]).await.unwrap();
            client.write_u64_le(MAX_TRANSFER + 1).await.unwrap();
            assert!(server.await.unwrap().unwrap_err().contains("limit"));

            let (mut client, mut agent) = tokio::io::duplex(1 << 16);
            let server = tokio::spawn(async move { serve(&mut agent).await });
            client.write_all(&[9]).await.unwrap();
            assert!(server.await.unwrap().unwrap_err().contains("version 9"));
        });
    }

    #[test]
    fn a_closed_connection_ends_the_test_cleanly() {
        rt().block_on(async {
            let (mut client, mut agent) = tokio::io::duplex(1 << 16);
            let server = tokio::spawn(async move { serve(&mut agent).await });
            client.write_all(&[VERSION]).await.unwrap();
            let mut hello = [0u8; 2];
            client.read_exact(&mut hello).await.unwrap();
            drop(client);
            assert_eq!(server.await.unwrap(), Ok(Moved::default()));
        });
    }
}
