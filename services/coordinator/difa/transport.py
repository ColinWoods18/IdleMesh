"""Mutual-TLS tunnels from the coordinator to remote inference workers (interim answer to DEV 08).

llama.cpp RPC is unencrypted and unauthenticated, so llama-server only ever talks to loopback. For each
remote stage the coordinator opens a local forwarder on 127.0.0.1 that carries the RPC stream over TLS
1.3 to the device agent's tunnel. Both ends pin certificates exchanged at enrollment: the device's
certificate is its enrolled public key, and the coordinator's certificate is sent in the enrollment
reply. The forwarders run on their own event loop thread so request handling cannot delay token
round trips.
"""
import asyncio
import datetime
import hashlib
import os
import socket
import ssl
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERT_HEADER = "-----BEGIN CERTIFICATE-----"


def cert_fingerprint(pem: str) -> str:
    """SHA-256 of the certificate's DER bytes, shown to owners so they can compare devices."""
    der = x509.load_pem_x509_certificate(pem.encode()).public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def is_certificate(pem: str) -> bool:
    if not pem.startswith(CERT_HEADER):
        return False
    try:
        x509.load_pem_x509_certificate(pem.encode())
        return True
    except ValueError:
        return False


class CoordinatorIdentity:
    """The coordinator's tunnel client key and self-signed certificate, created once per data directory."""

    def __init__(self, directory: str):
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        self.key_path, self.cert_path = d / "coordinator-tunnel.key", d / "coordinator-tunnel.crt"
        if not (self.key_path.exists() and self.cert_path.exists()):
            self._create()
        self.cert_pem = self.cert_path.read_text()

    def _create(self) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DIFA coordinator tunnel")])
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(start)
                .not_valid_after(start + datetime.timedelta(days=825))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("difa-coordinator")]), critical=False)
                .sign(key, hashes.SHA256()))
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    def client_context(self, device_cert_pem: str) -> ssl.SSLContext:
        """TLS 1.3 client that presents our certificate and trusts only this one device certificate."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.check_hostname = False          # identity is the pinned certificate, not a host name
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=device_cert_pem)
        ctx.load_cert_chain(self.cert_path, self.key_path)
        return ctx


class TunnelError(Exception):
    pass


class _Forwarder:
    def __init__(self, remote: str, ctx: ssl.SSLContext, expected_der: bytes):
        host, _, port = remote.rpartition(":")
        self.host, self.port = host.strip("[]"), int(port)
        self.ctx, self.expected_der = ctx, expected_der
        self.server: asyncio.AbstractServer | None = None
        self.local_port = 0
        self.connections = 0
        self.bytes_to_device = 0
        self.writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.local_port = self.server.sockets[0].getsockname()[1]

    async def _pipe(self, r: asyncio.StreamReader, w: asyncio.StreamWriter, outbound: bool = False) -> None:
        try:
            while data := await r.read(1 << 16):
                if outbound:
                    self.bytes_to_device += len(data)
                w.write(data)
                await w.drain()
        except (ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            w.close()

    async def _handle(self, lr: asyncio.StreamReader, lw: asyncio.StreamWriter) -> None:
        try:
            rr, rw = await asyncio.wait_for(asyncio.open_connection(self.host, self.port, ssl=self.ctx), 10)
        except (OSError, ssl.SSLError, asyncio.TimeoutError):
            lw.close()
            return
        peer = rw.get_extra_info("ssl_object").getpeercert(binary_form=True)
        if peer != self.expected_der:  # trust store holds only this certificate; check anyway
            rw.close()
            lw.close()
            return
        self.connections += 1
        for w in (lw, rw):
            sock = w.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.writers.update((lw, rw))
        await asyncio.gather(self._pipe(lr, rw, outbound=True), self._pipe(rr, lw), return_exceptions=True)
        self.writers.difference_update((lw, rw))

    async def stop(self) -> None:
        if self.server:
            self.server.close()
        for w in list(self.writers):
            w.close()
        if self.server:
            await self.server.wait_closed()


class TunnelManager:
    """Opens and closes per-deployment forwarders on a dedicated event loop thread."""

    def __init__(self, identity: CoordinatorIdentity):
        self.identity = identity
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="difa-tunnels", daemon=True)
        self.thread.start()
        self._open: dict[str, list[_Forwarder]] = {}

    def _call(self, coro, timeout: float = 15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def open(self, owner: str, remote: str, device_cert_pem: str) -> str:
        """Start a forwarder for one remote stage of deployment `owner`; returns its loopback endpoint."""
        if not is_certificate(device_cert_pem):
            raise TunnelError("device has no enrolled certificate; re-enroll it with a current agent")
        der = x509.load_pem_x509_certificate(device_cert_pem.encode()).public_bytes(serialization.Encoding.DER)
        fwd = _Forwarder(remote, self.identity.client_context(device_cert_pem), der)
        self._call(fwd.start())
        self._open.setdefault(owner, []).append(fwd)
        return f"127.0.0.1:{fwd.local_port}"

    def probe(self, remote: str, device_cert_pem: str, timeout: float = 5) -> None:
        """Complete one handshake to check reachability and identity before a deployment loads."""
        async def go():
            host, _, port = remote.rpartition(":")
            _, w = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(port),
                                                                  ssl=self.identity.client_context(device_cert_pem)),
                                          timeout)
            w.close()
        try:
            self._call(go(), timeout + 1)
        except ssl.SSLError as e:
            raise TunnelError(f"TLS handshake with {remote} failed: {e.reason or e}") from None
        except (OSError, asyncio.TimeoutError, TimeoutError) as e:
            raise TunnelError(f"cannot reach the worker tunnel at {remote}: {e or 'timeout'}") from None

    def close(self, owner: str) -> None:
        for fwd in self._open.pop(owner, []):
            try:
                self._call(fwd.stop(), 5)
            except Exception:  # noqa: BLE001 - best effort on teardown
                pass

    def close_all(self) -> None:
        for owner in list(self._open):
            self.close(owner)

    def connections(self, owner: str) -> int:
        return sum(f.connections for f in self._open.get(owner, []))

    def bytes_to_devices(self, owner: str) -> int:
        """Bytes llama-server sent to the remote workers of this deployment (weights, graphs, inputs)."""
        return sum(f.bytes_to_device for f in self._open.get(owner, []))
