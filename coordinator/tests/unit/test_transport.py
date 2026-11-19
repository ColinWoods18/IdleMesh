"""Coordinator side of the worker tunnel: pinning, refusal of impostors, and byte-exact forwarding."""
import asyncio
import socket
import ssl
import struct
import threading

import pytest

from idlemesh_coord.transport import (CoordinatorIdentity, TunnelError, TunnelManager, cert_fingerprint, is_certificate,
                            rpc_command, rtt_summary)


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


class FakeRpcWorker(EchoTLSServer):
    """A device tunnel in front of a llama.cpp RPC worker that answers HELLO and DEVICE_COUNT after `delay_s`."""

    def __init__(self, device: CoordinatorIdentity, client_cert_pem: str, delay_s: float, major: int = 8):
        super().__init__(device, client_cert_pem)
        self.server.close()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(device.cert_path, device.key_path)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=client_cert_pem)

        async def handle(r, w):
            try:
                while True:
                    cmd = (await r.readexactly(1))[0]
                    (size,) = struct.unpack("<Q", await r.readexactly(8))
                    await r.readexactly(size)
                    await asyncio.sleep(delay_s)
                    reply = bytes([major, 0, 1, 0]) + bytes(24) if cmd == 14 else struct.pack("<I", 1)
                    w.write(struct.pack("<Q", len(reply)) + reply)
                    await w.drain()
            except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
                pass
            w.close()

        async def start():
            return await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
        self.server = asyncio.run_coroutine_threadsafe(start(), self.loop).result(5)
        self.endpoint = f"127.0.0.1:{self.server.sockets[0].getsockname()[1]}"


def test_rpc_framing_matches_llama_cpp():
    assert rpc_command(14, bytes(24)) == b"\x0e" + struct.pack("<Q", 24) + bytes(24)
    assert rpc_command(15, b"") == b"\x0f" + bytes(8)
    s = rtt_summary([10.0, 12.0, 11.0, 40.0])
    assert s["mean_ms"] == 18.25 and s["median_ms"] == 11.5 and s["min_ms"] == 10.0 and s["samples"] == 4


def test_measure_rtt_times_rpc_round_trips_through_the_tunnel(ids):
    coord, device, _ = ids
    worker = FakeRpcWorker(device, coord.cert_pem, delay_s=0.02)
    try:
        m = TunnelManager(coord).measure_rtt(worker.endpoint, device.cert_pem, samples=8)
        assert m["samples"] == 8 and 19 <= m["min_ms"] and m["mean_ms"] < 60, m
    finally:
        worker.close()


def test_measure_rtt_rejects_another_rpc_protocol_and_impostors(ids):
    coord, device, impostor = ids
    old = FakeRpcWorker(device, coord.cert_pem, delay_s=0, major=3)
    fake = FakeRpcWorker(impostor, coord.cert_pem, delay_s=0)
    try:
        with pytest.raises(TunnelError, match="RPC protocol 3.0.1"):
            TunnelManager(coord).measure_rtt(old.endpoint, device.cert_pem, samples=2)
        with pytest.raises(TunnelError, match="handshake"):
            TunnelManager(coord).measure_rtt(fake.endpoint, device.cert_pem, samples=2)
    finally:
        old.close()
        fake.close()
