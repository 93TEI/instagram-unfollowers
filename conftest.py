"""All Python tests use synthetic data and prohibit outgoing network connections."""

import socket

import pytest


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Tests must not connect to real accounts or external services")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
