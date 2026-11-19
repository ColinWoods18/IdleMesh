//! Control channel to the coordinator: HTTPS with the coordinator's certificate pinned by SHA-256.
//!
//! Enrollment, heartbeats, and weight downloads from the coordinator all carry the device token, so
//! they must not travel in clear text or reach an impostor. The coordinator's certificate is
//! self-signed (there is no certificate authority on a home network). Trust comes from the enrollment
//! code instead: the owner reads the certificate's fingerprint off the coordinator together with the
//! code, and the agent refuses to send the code unless the server presents a certificate with that
//! fingerprint. After enrollment the agent stores the full fingerprint and accepts only that
//! certificate. Plain HTTP is allowed only to a loopback coordinator (the coordinator's own machine
//! and tests).

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{verify_tls12_signature, verify_tls13_signature, WebPkiSupportedAlgorithms};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{CertificateError, DigitallySignedStruct, Error, SignatureScheme};
use sha2::{Digest, Sha256};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// Fewest hex digits accepted from an owner at enrollment: 64 bits, far beyond what an attacker
/// could match during a code's 10-minute life.
pub const MIN_FINGERPRINT_HEX: usize = 16;

/// Lower-case hex, with the separators people copy (spaces, colons, dashes) removed.
pub fn normalize_fingerprint(s: &str) -> Result<String, String> {
    let hex: String = s.chars().filter(|c| !matches!(c, ' ' | ':' | '-')).collect::<String>().to_lowercase();
    if !hex.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err("the coordinator fingerprint must be hexadecimal".into());
    }
    if hex.len() < MIN_FINGERPRINT_HEX || hex.len() > 64 {
        return Err(format!("the coordinator fingerprint must have {MIN_FINGERPRINT_HEX} to 64 hex digits"));
    }
    Ok(hex)
}

pub fn sha256_hex(der: &[u8]) -> String {
    Sha256::digest(der).iter().map(|b| format!("{b:02x}")).collect()
}

/// Accepts one server certificate: the one whose SHA-256 starts with the pinned fingerprint. Records
/// the full fingerprint it saw so enrollment can pin all 64 digits from then on.
#[derive(Debug)]
pub struct PinnedServer {
    fingerprint: String,
    algorithms: WebPkiSupportedAlgorithms,
    pub seen: Seen,
}

impl PinnedServer {
    pub fn new(fingerprint: &str) -> Self {
        PinnedServer { fingerprint: fingerprint.to_string(),
                       algorithms: rustls::crypto::ring::default_provider().signature_verification_algorithms,
                       seen: Arc::new(Mutex::new(None)) }
    }
}

impl ServerCertVerifier for PinnedServer {
    fn verify_server_cert(&self, end_entity: &CertificateDer<'_>, _intermediates: &[CertificateDer<'_>],
                          _server_name: &ServerName<'_>, _ocsp: &[u8], _now: UnixTime)
                          -> Result<ServerCertVerified, Error> {
        let got = sha256_hex(end_entity.as_ref());
        if got.starts_with(&self.fingerprint) {
            *self.seen.lock().unwrap() = Some(got);
            Ok(ServerCertVerified::assertion())
        } else {
            Err(Error::InvalidCertificate(CertificateError::ApplicationVerificationFailure))
        }
    }

    fn verify_tls12_signature(&self, message: &[u8], cert: &CertificateDer<'_>, dss: &DigitallySignedStruct)
                              -> Result<HandshakeSignatureValid, Error> {
        verify_tls12_signature(message, cert, dss, &self.algorithms)
    }

    fn verify_tls13_signature(&self, message: &[u8], cert: &CertificateDer<'_>, dss: &DigitallySignedStruct)
                              -> Result<HandshakeSignatureValid, Error> {
        verify_tls13_signature(message, cert, dss, &self.algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algorithms.supported_schemes()
    }
}

/// Host part of an http(s) URL, without brackets or port.
fn host_of(url: &str) -> &str {
    let rest = url.split_once("://").map_or(url, |(_, r)| r);
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let authority = authority.rsplit_once('@').map_or(authority, |(_, h)| h);
    if let Some(v6) = authority.strip_prefix('[') {
        return v6.split(']').next().unwrap_or("");
    }
    authority.split(':').next().unwrap_or("")
}

pub fn is_loopback_url(url: &str) -> bool {
    let host = host_of(url);
    if host == "localhost" {
        return true;
    }
    host.parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

/// Where the verifier records the full fingerprint it accepted.
pub type Seen = Arc<Mutex<Option<String>>>;

/// A client for the coordinator. With https it pins `fingerprint`; with http it allows loopback only.
/// Returns the client and the slot where the verifier records the full fingerprint it accepted.
pub fn client(coordinator: &str, fingerprint: Option<&str>, connect: Duration, total: Duration)
              -> Result<(reqwest::blocking::Client, Seen), String> {
    let builder = reqwest::blocking::Client::builder().connect_timeout(connect).timeout(total);
    if coordinator.starts_with("http://") {
        if !is_loopback_url(coordinator) {
            return Err("refusing plain HTTP to a coordinator on the network: the device token would travel in \
                        clear text. Use the https:// address and --coordinator-fingerprint shown by the coordinator"
                .into());
        }
        let c = builder.build().map_err(|e| e.to_string())?;
        return Ok((c, Arc::new(Mutex::new(None))));
    }
    if !coordinator.starts_with("https://") {
        return Err("the coordinator address must start with https://".into());
    }
    let Some(fp) = fingerprint else {
        return Err("an https coordinator needs its certificate fingerprint (--coordinator-fingerprint)".into());
    };
    let verifier = Arc::new(PinnedServer::new(&normalize_fingerprint(fp)?));
    let seen = verifier.seen.clone();
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let tls = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| e.to_string())?
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();
    let c = builder.use_preconfigured_tls(tls).build().map_err(|e| e.to_string())?;
    Ok((c, seen))
}

/// The coordinator's own handling time from a `Server-Timing: app;dur=<ms>` header, or 0 if absent.
pub fn server_time_ms(header: Option<&str>) -> f64 {
    header
        .and_then(|h| h.split([',', ';'])
            .find_map(|p| p.trim().strip_prefix("dur=").and_then(|v| v.parse::<f64>().ok())))
        .filter(|v| v.is_finite() && *v >= 0.0)
        .unwrap_or(0.0)
}

/// Network round trip from recent heartbeats: each sample is the request time minus the coordinator's
/// handling time, and the reported value is the median of the last few, so one slow heartbeat (a new
/// TLS connection, a Wi-Fi stall) does not move the planner's fallback RTT.
pub struct RttWindow {
    samples: std::collections::VecDeque<f64>,
    cap: usize,
}

impl RttWindow {
    pub fn new(cap: usize) -> Self {
        RttWindow { samples: std::collections::VecDeque::with_capacity(cap), cap }
    }

    pub fn push(&mut self, elapsed_ms: f64, server_ms: f64) -> f64 {
        if self.samples.len() == self.cap {
            self.samples.pop_front();
        }
        self.samples.push_back((elapsed_ms - server_ms).max(0.0));
        self.median()
    }

    pub fn median(&self) -> f64 {
        let mut v: Vec<f64> = self.samples.iter().copied().collect();
        if v.is_empty() {
            return 0.0;
        }
        v.sort_by(|a, b| a.total_cmp(b));
        let m = v.len() / 2;
        let x = if v.len() % 2 == 1 { v[m] } else { (v[m - 1] + v[m]) / 2.0 };
        (x * 10.0).round() / 10.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heartbeat_rtt_excludes_server_time_and_outliers() {
        assert_eq!(server_time_ms(Some("app;dur=12.50")), 12.5);
        assert_eq!(server_time_ms(Some("db;dur=x, app;dur=3")), 3.0);
        assert_eq!(server_time_ms(Some("app;dur=-4")), 0.0);
        assert_eq!(server_time_ms(None), 0.0);
        let mut w = RttWindow::new(5);
        assert_eq!(w.push(190.0, 10.0), 180.0); // first request: new TLS connection
        for _ in 0..4 {
            w.push(22.0, 4.0);
        }
        assert_eq!(w.median(), 18.0);
        assert_eq!(w.push(15.0, 20.0), 18.0); // never negative, and the cold sample has aged out
    }

    #[test]
    fn fingerprints_are_normalized_and_bounded() {
        assert_eq!(normalize_fingerprint("AB:cd 12-34 5678 9abc").unwrap(), "abcd123456789abc");
        assert!(normalize_fingerprint("abcd").is_err());
        assert!(normalize_fingerprint("zz34567890abcdef").is_err());
        assert!(normalize_fingerprint(&"a".repeat(65)).is_err());
    }

    #[test]
    fn plain_http_only_to_loopback() {
        let t = Duration::from_secs(1);
        assert!(client("http://127.0.0.1:8765", None, t, t).is_ok());
        assert!(client("http://[::1]:8765", None, t, t).is_ok());
        let err = client("http://192.168.1.10:8765", None, t, t).unwrap_err();
        assert!(err.contains("refusing plain HTTP"), "{err}");
        assert!(client("https://192.168.1.10:8765", None, t, t).unwrap_err().contains("fingerprint"));
        assert!(client("https://192.168.1.10:8765", Some("00112233445566778899"), t, t).is_ok());
        assert!(!is_loopback_url("http://127.0.0.1.evil.example:80"));
        assert!(!is_loopback_url("http://user@10.0.0.1:80"));
        assert!(is_loopback_url("http://localhost:8765/api"));
    }

    #[test]
    fn verifier_accepts_only_the_pinned_certificate() {
        let a = crate::tunnel::generate_identity("a").unwrap();
        let b = crate::tunnel::generate_identity("b").unwrap();
        let (da, db) = (crate::tunnel::cert_der(&a.cert_pem).unwrap(), crate::tunnel::cert_der(&b.cert_pem).unwrap());
        let full = sha256_hex(da.as_ref());
        let v = PinnedServer::new(&full[..16]);
        let name = ServerName::try_from("difa-coordinator").unwrap();
        assert!(v.verify_server_cert(&db, &[], &name, &[], UnixTime::now()).is_err());
        assert!(v.seen.lock().unwrap().is_none());
        assert!(v.verify_server_cert(&da, &[], &name, &[], UnixTime::now()).is_ok());
        assert_eq!(v.seen.lock().unwrap().as_deref(), Some(full.as_str()));
    }
}
