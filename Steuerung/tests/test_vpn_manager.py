"""Regressionstests fuer die nicht-blockierende WireGuard-Statuspruefung."""
import asyncio
from types import SimpleNamespace

import pytest

import vpn_manager


class _Process:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = None if hang else returncode
        self.hang = hang
        self.killed = False

    async def communicate(self):
        if self.hang and not self.killed:
            await asyncio.Event().wait()
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True
        self.returncode = -9


def _patch_linux(monkeypatch):
    monkeypatch.setattr(vpn_manager.sys, "platform", "linux")


@pytest.mark.asyncio
async def test_vpn_status_verwendet_exec_und_erste_ipv4(monkeypatch):
    _patch_linux(monkeypatch)
    process = _Process(
        stdout=(
            b"2: wg0    inet 10.8.0.2/24 scope global wg0\n"
            b"2: wg0    inet 10.8.0.99/24 scope global wg0\n"
        )
    )
    aufrufe = []

    async def create_exec(*args, **kwargs):
        aufrufe.append((args, kwargs))
        return process

    async def create_shell(*args, **kwargs):
        pytest.fail("VPN-Status darf nicht ueber eine Shell ausgefuehrt werden")

    monkeypatch.setattr(vpn_manager.asyncio, "create_subprocess_exec", create_exec)
    monkeypatch.setattr(vpn_manager.asyncio, "create_subprocess_shell", create_shell)
    state = SimpleNamespace(vpn_ip=None)

    await vpn_manager.check_vpn_status(state)

    assert state.vpn_ip == "10.8.0.2"
    assert aufrufe[0][0] == (
        "ip", "-4", "-o", "addr", "show", "dev", "wg0"
    )


@pytest.mark.asyncio
async def test_vpn_status_bei_kommandofehler_trennt(monkeypatch):
    _patch_linux(monkeypatch)
    process = _Process(stderr=b"Device does not exist", returncode=1)

    async def create_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(vpn_manager.asyncio, "create_subprocess_exec", create_exec)
    state = SimpleNamespace(vpn_ip="10.8.0.2")

    await vpn_manager.check_vpn_status(state)

    assert state.vpn_ip is None


@pytest.mark.asyncio
async def test_vpn_status_beendet_prozess_bei_timeout(monkeypatch):
    _patch_linux(monkeypatch)
    monkeypatch.setattr(vpn_manager, "VPN_CHECK_TIMEOUT_SEC", 0.01)
    process = _Process(hang=True)

    async def create_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(vpn_manager.asyncio, "create_subprocess_exec", create_exec)
    state = SimpleNamespace(vpn_ip="10.8.0.2")

    await vpn_manager.check_vpn_status(state)

    assert process.killed is True
    assert state.vpn_ip is None
