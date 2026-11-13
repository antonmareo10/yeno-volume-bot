"""Bot factories for the local harness.

The official House Bot v1 and starter bot are *not* vendored in this repo
(they belong to Builderr).  Point ``--kit`` at a folder containing the
downloaded ``yeno-house-bot-v1.py`` / ``yeno_competition_starter_bot.py`` to
compare against them under the same harness.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

import yeno_volume_bot as ccb

Factory = Callable[[], Callable[[dict[str, Any]], Any]]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_factory(**overrides: Any) -> Factory:
    config = ccb.Config().updated(**overrides)
    return lambda: ccb.CleanCrossingBot(config).decide


def house_factory(kit: Path) -> Factory:
    module = _load(kit / "yeno-house-bot-v1.py", "yeno_house_bot_v1")
    return lambda: module.HouseBot().decide


def starter_factory(kit: Path) -> Factory:
    module = _load(kit / "yeno_competition_starter_bot.py", "yeno_starter_bot")
    return lambda: module.decide


def http_factory(url: str, timeout: float = 1.0) -> Factory:
    """Drive any /decide endpoint over HTTP (the way the official run does)."""
    import json
    import urllib.request

    def factory():
        def decide(observation: dict[str, Any]) -> Any:
            request = urllib.request.Request(
                url,
                data=json.dumps(observation).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())

        return decide

    return factory
