import copy

import pytest

from radar import aisa, backend, config, dotcom, glasser, goanyapi, i18n, page_age


@pytest.fixture(autouse=True)
def english(monkeypatch):
    i18n.set_lang("en")
    monkeypatch.delenv("GOANYAPI_API_KEY", raising=False)   # the optional source is off unless a test turns it on
    # Glasser and a model endpoint are off unless a test turns them on, and so is an AIsa key on the machine
    for name in ("GLASSER_API_KEY", "RADAR_LLM_BASE_URL", "RADAR_LLM_API_KEY", "RADAR_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(backend, "_aisa_key", lambda: "")

    def no_network(*a, **k):
        raise goanyapi.GoAnyError("no network in tests")
    monkeypatch.setattr(goanyapi, "call", no_network)

    def no_paid_calls(*a, **k):       # a key on the machine must never turn a test into a paid call
        raise aisa.AisaError("no network in tests")
    monkeypatch.setattr(aisa, "_post", no_paid_calls)

    def no_paid_runs(*a, **k):        # Glasser's Runs and a model endpoint of your own
        raise OSError("no network in tests")
    monkeypatch.setattr(glasser._OPENER, "open", no_paid_runs)
    monkeypatch.setattr(backend._OPENER, "open", no_paid_runs)
    monkeypatch.setattr(page_age, "ask", lambda domain: ("", "none"))      # RDAP: a test that needs it stubs it

    def no_rdap(url):                 # the .com lookup is free, but a test never reaches the registry
        raise OSError("no network in tests")
    monkeypatch.setattr(dotcom, "_get", no_rdap)
    # The page-one check runs only in the tests that set it up, whatever loads the config (the MCP server too).
    monkeypatch.setitem(config.DEFAULTS["competition"], "max_rows", 0)
    for block in ("word_age", "markets", "serp_shape", "dot_com"):       # 0.6's extra readings and 0.7's .com: on in the tests that set them up
        monkeypatch.setitem(config.DEFAULTS[block], "enabled", False)
    monkeypatch.setitem(config.DEFAULTS["asks"], "weekday", None)          # weekly signals stay off unless a test asks
    monkeypatch.setitem(config.DEFAULTS["tutorials"], "max_leads", 0)
    monkeypatch.setitem(config.DEFAULTS["sellers"], "weekday", None)
    # 0.5's free sources reach the network without a key: off unless a test turns them on
    monkeypatch.setitem(config.DEFAULTS["builders"], "weekday", None)
    monkeypatch.setitem(config.DEFAULTS["money"], "weekday", None)
    monkeypatch.setitem(config.DEFAULTS["feeds"], "enabled", [])
    monkeypatch.setitem(config.DEFAULTS["sidelights"], "names", 0)
    monkeypatch.setitem(config.DEFAULTS["sidelights"], "products", 0)
    monkeypatch.setitem(config.DEFAULTS["pains"], "apps", 0)
    monkeypatch.setitem(config.DEFAULTS["asks"], "extra_sources", [])


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(config.DEFAULTS)
    c["_dir"] = tmp_path
    c["competition"]["max_rows"] = 0     # the page-one check runs only in the tests that set it up
    return c
