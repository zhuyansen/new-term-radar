"""Select a data backend per capability; chat and X remain on AIsa."""
from __future__ import annotations

from . import aisa, glasser

DEFAULTS = {"trends": "aisa", "volume": "aisa"}


def validate(cfg: dict) -> None:
    providers = cfg.get("data_providers", {})
    if not isinstance(providers, dict) or providers.keys() - DEFAULTS.keys():
        raise ValueError("data_providers supports only trends and volume; X, new sites and chat use AIsa")
    if any(value not in ("aisa", "glasser") for value in providers.values()):
        raise ValueError("data_providers values must be aisa or glasser")
    if "glasser" in providers.values():
        glasser.settings(cfg.get("glasser", {}))


def call_all(calls: list[dict], cfg: dict | None = None, *, capability: str) -> tuple[dict[str, dict], int]:
    cfg = cfg or {}
    validate(cfg)
    if capability not in DEFAULTS:
        raise ValueError(f"unsupported data capability: {capability}")
    if cfg.get("data_providers", {}).get(capability, "aisa") == "glasser":
        return glasser.call_all(calls, capability, cfg.get("glasser", {}))
    # Preserve the original AIsa single-batch volume and multi-batch Trends calls.
    return aisa.call(calls) if capability == "volume" else aisa.call_all(calls)
