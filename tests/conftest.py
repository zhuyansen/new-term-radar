import copy
import socket

import pytest

from radar import config, i18n


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests must never make real provider calls, including when keys are set locally."""
    def blocked(*args, **kwargs):
        raise AssertionError("network access is disabled in tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.delenv("AISA_API_KEY", raising=False)
    monkeypatch.delenv("GLASSER_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def english():
    i18n.set_lang("en")


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(config.DEFAULTS)
    c["_dir"] = tmp_path
    return c
