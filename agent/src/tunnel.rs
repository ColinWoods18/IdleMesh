//! Mutual-TLS tunnel in front of the inference worker (interim answer to decision DEV 08).
//!
//! llama.cpp RPC has no encryption or authentication, so the worker never listens on the network. It
//! binds to loopback, and this tunnel is the only network-facing listener: TLS 1.3, and both ends
//! present a certificate pinned at enrollment. The device certificate is generated here and sent to
//! the coordinator as the device's public key; the coordinator's certificate comes back in the
//! enrollment reply. A connection from anything else fails the handshake and never reaches the
//! worker. Certificates are pinned by exact bytes, so there is no certificate authority to compromise;
//! removing a device on the coordinator stops it being dialled, and revocation stops the agent.

use rustls::client::danger::HandshakeSignatureValid;
use rustls::crypto::{verify_tls12_signature, verify_tls13_signature, CryptoProvider, WebPkiSupportedAlgorithms};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, UnixTime};
use rustls::server::danger::{ClientCertVerified, ClientCertVerifier};
use rustls::{CertificateError, DigitallySignedStruct, DistinguishedName, Error, ServerConfig, SignatureScheme};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

pub struct Identity {
    pub cert_pem: String,
    pub key_pem: String,
}

/// A fresh ECDSA P-256 key and self-signed certificate for this device.
pub fn generate_identity(device_name: &str) -> Result<Identity, String> {
    let mut params = rcgen::CertificateParams::new(vec!["difa-device".to_string()]).map_err(|e| e.to_string())?;
    params.distinguished_name.push(rcgen::DnType::CommonName, format!("DIFA device {device_name}"));
    let key = rcgen::KeyPair::generate().map_err(|e| e.to_string())?;
    let cert = params.self_signed(&key).map_err(|e| e.to_string())?;
    Ok(Identity { cert_pem: cert.pem(), key_pem: key.serialize_pem() })
}

pub fn cert_der(pem: &str) -> Result<CertificateDer<'static>, String> {
    CertificateDer::from_pem_slice(pem.as_bytes()).map_err(|e| format!("bad certificate: {e:?}"))
}

/// Accepts exactly one client certificate, compared byte for byte, and checks the handshake signature.
#[derive(Debug)]
pub struct PinnedClient {
    cert: CertificateDer<'static>,
    algorithms: WebPkiSupportedAlgorithms,
}

impl PinnedClient {
    pub fn new(cert: CertificateDer<'static>, provider: &CryptoProvider) -> Self {
        PinnedClient { cert, algorithms: provider.signature_verification_algorithms }
    }
}

impl ClientCertVerifier for PinnedClient {
    fn root_hint_subjects(&self) -> &[DistinguishedName] {
        &[]
    }

    fn verify_client_cert(&self, end_entity: &CertificateDer<'_>, intermediates: &[CertificateDer<'_>],
                          _now: UnixTime) -> Result<ClientCertVerified, Error> {
        if intermediates.is_empty() && end_entity.as_ref() == self.cert.as_ref() {
            Ok(ClientCertVerified::assertion())
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

/// TLS 1.3 only, client certificate required and pinned.
pub fn server_config(device: &Identity, coordinator_cert_pem: &str) -> Result<ServerConfig, String> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let verifier = Arc::new(PinnedClient::new(cert_der(coordinator_cert_pem)?, &provider));
    let key = PrivateKeyDer::from_pem_slice(device.key_pem.as_bytes()).map_err(|e| format!("bad key: {e:?}"))?;
    ServerConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map_err(|e| e.to_string())?
        .with_client_cert_verifier(verifier)
        .with_single_cert(vec![cert_der(&device.cert_pem)?], key)
        .map_err(|e| e.to_string())
}

#[derive(Default)]
pub struct Counters {
    pub accepted: AtomicU64,
    pub rejected: AtomicU64,
}

/// Bind the listener now (so a bad address fails at startup) and serve on a background thread.
pub fn spawn(listen: &str, worker_port: u16, config: ServerConfig) -> Result<Arc<Counters>, String> {
    let std_listener = std::net::TcpListener::bind(listen).map_err(|e| format!("tunnel cannot listen on {listen}: {e}"))?;
    std_listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let counters = Arc::new(Counters::default());
    let c = counters.clone();
    let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(config));
    std::thread::Builder::new()
        .name("idlemesh-tunnel".into())
        .spawn(move || {
            let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build()
                .expect("tunnel runtime");
            rt.block_on(async move {
                let listener = tokio::net::TcpListener::from_std(std_listener).expect("tunnel listener");
                loop {
                    let Ok((stream, peer)) = listener.accept().await else { continue };
                    let (acceptor, c) = (acceptor.clone(), c.clone());
                    tokio::spawn(async move {
                        let _ = stream.set_nodelay(true);
                        let mut tls = match tokio::time::timeout(Duration::from_secs(10), acceptor.accept(stream)).await {
                            Ok(Ok(tls)) => tls,
                            Ok(Err(e)) => {
                                c.rejected.fetch_add(1, Ordering::Relaxed);
                                tracing::warn!(error_code = "TUNNEL_HANDSHAKE_FAILED", peer = %peer,
                                               "tunnel: rejected {peer}: {e}");
                                return;
                            }
                            Err(_) => {
                                c.rejected.fetch_add(1, Ordering::Relaxed);
                                tracing::warn!(error_code = "TUNNEL_HANDSHAKE_TIMEOUT", peer = %peer,
                                               "tunnel: handshake timeout from {peer}");
                                return;
                            }
                        };
                        match tokio::net::TcpStream::connect(("127.0.0.1", worker_port)).await {
                            Ok(mut upstream) => {
                                c.accepted.fetch_add(1, Ordering::Relaxed);
                                let _ = upstream.set_nodelay(true);
                                let _ = tokio::io::copy_bidirectional(&mut tls, &mut upstream).await;
                            }
                            Err(e) => tracing::warn!(error_code = "TUNNEL_WORKER_UNREACHABLE",
                                                     "tunnel: worker not reachable on loopback: {e}"),
                        }
                    });
                }
            });
        })
        .map_err(|e| e.to_string())?;
    Ok(counters)
}

/// Loopback-only check used to refuse exposing the plaintext worker on a network.
pub fn is_loopback_host(host: &str) -> bool {
    host == "localhost" || host.parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_identity_builds_a_server_config() {
        let dev = generate_identity("test").unwrap();
        let coord = generate_identity("coordinator").unwrap();
        assert!(dev.cert_pem.starts_with("-----BEGIN CERTIFICATE-----"));
        assert!(server_config(&dev, &coord.cert_pem).is_ok());
    }

    #[test]
    fn pinned_verifier_accepts_only_the_pinned_certificate() {
        let provider = rustls::crypto::ring::default_provider();
        let good = cert_der(&generate_identity("a").unwrap().cert_pem).unwrap();
        let other = cert_der(&generate_identity("b").unwrap().cert_pem).unwrap();
        let v = PinnedClient::new(good.clone(), &provider);
        let now = UnixTime::now();
        assert!(v.verify_client_cert(&good, &[], now).is_ok());
        assert!(v.verify_client_cert(&other, &[], now).is_err());
        assert!(v.verify_client_cert(&good, std::slice::from_ref(&other), now).is_err());
    }

    #[test]
    fn loopback_detection() {
        assert!(is_loopback_host("127.0.0.1") && is_loopback_host("::1") && is_loopback_host("localhost"));
        assert!(!is_loopback_host("0.0.0.0") && !is_loopback_host("192.168.1.20"));
    }
}
