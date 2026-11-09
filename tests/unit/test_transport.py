"""Coordinator side of the worker tunnel: pinning, refusal of impostors, and byte-exact forwarding."""
import asyncio
import socket
import ssl
import threading

import pytest

from difa.transport import CoordinatorIdentity, TunnelError, TunnelManager, cert_fingerprint, is_certificate


class EchoTLSServer:
    """Stands in for a device tunnel: presents `device`'s certificate and requires `client_cert_pem`."""

    def __init__(self, device: CoordinatorIdentity, client_cert_pem: str):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(device.cert_path, device.key_path)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=client_cert_pem)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        async def handle(r, w):
            try:
                while data := await r.read(65536):
                    w.write(data)
                    await w.drain()
            except (ConnectionError, ssl.SSLError):
                pass
            w.close()

        async def start():
            return await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
        self.server = asyncio.run_coroutine_threadsafe(start(), self.loop).result(5)
        self.endpoint = f"127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    def close(self):
        self.loop.call_soon_threadsafe(self.server.close)


@pytest.fixture
def ids(tmp_path):
    return (CoordinatorIdentity(str(tmp_path / "coord")), CoordinatorIdentity(str(tmp_path / "device")),
            CoordinatorIdentity(str(tmp_path / "impostor")))


def test_identity_is_created_once_with_a_private_key(tmp_path):
    a = CoordinatorIdentity(str(tmp_path))
    b = CoordinatorIdentity(str(tmp_path))
    assert a.cert_pem == b.cert_pem and is_certificate(a.cert_pem)
    assert (a.key_path.stat().st_mode & 0o777) == 0o600
    assert len(cert_fingerprint(a.cert_pem)) == 64
    assert not is_certificate("k") and not is_certificate("-----BEGIN CERTIFICATE-----\nnot base64\n")


def test_forwarder_carries_bytes_to_the_pinned_device(ids):
    coord, device, _ = ids
    server = EchoTLSServer(device, coord.cert_pem)
    tunnels = TunnelManager(coord)
    try:
        tunnels.probe(server.endpoint, device.cert_pem)
        local = tunnels.open("dep_1", server.endpoint, device.cert_pem)
        host, port = local.split(":")
        payload = bytes(range(256)) * 1024
        with socket.create_connection((host, int(port)), timeout=5) as s:
            s.sendall(payload)
            got = b""
            while len(got) < len(payload):
                got += s.recv(65536)
        assert got == payload and tunnels.connections("dep_1") == 1
    finally:
        tunnels.close_all()
        server.close()


def test_impostor_device_is_refused(ids):
    coord, device, impostor = ids
    server = EchoTLSServer(impostor, coord.cert_pem)  # right address, wrong certificate
    tunnels = TunnelManager(coord)
    try:
        with pytest.raises(TunnelError, match="handshake"):
            tunnels.probe(server.endpoint, device.cert_pem)
    finally:
        server.close()


def test_device_that_does_not_trust_us_is_refused(ids):
    coord, device, impostor = ids
    server = EchoTLSServer(device, impostor.cert_pem)  # device pinned a different coordinator
    tunnels = TunnelManager(coord)
    try:
        local = tunnels.open("dep_2", server.endpoint, device.cert_pem)
        host, port = local.split(":")
        with socket.create_connection((host, int(port)), timeout=5) as s:
            s.sendall(b"hello")
            s.settimeout(5)
            assert s.recv(16) == b""  # forwarder closes: nothing reaches the worker
    finally:
        tunnels.close_all()
        server.close()


def test_unreachable_tunnel_and_missing_certificate(ids):
    coord, device, _ = ids
    tunnels = TunnelManager(coord)
    with pytest.raises(TunnelError, match="cannot reach"):
        tunnels.probe("127.0.0.1:1", device.cert_pem, timeout=1)
    with pytest.raises(TunnelError, match="no enrolled certificate"):
        tunnels.open("dep_3", "127.0.0.1:1", "placeholder")
