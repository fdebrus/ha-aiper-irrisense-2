"""Shared pytest configuration for the Aiper Irrisense 2 test suite.

The tests here are deliberately split between two kinds:

* **Pure-logic unit tests** (crypto envelope, const preset maps, the
  coordinator's frame-parsing helpers) that import the module directly and
  never spin up Home Assistant. These form the regression net that pins the
  reverse-engineered wire behaviour byte-for-byte, and they stay fast and
  dependency-light because they don't touch the ``hass`` fixture.
* **HA-aware tests** that need the ``hass`` fixture / custom-integration
  loader from ``pytest-homeassistant-custom-component``. Those tests should
  request the plugin's ``enable_custom_integrations`` fixture explicitly:

      async def test_setup(hass, enable_custom_integrations): ...

We intentionally do **not** make ``enable_custom_integrations`` autouse: it
pulls in the ``hass`` fixture, and forcing a full Home Assistant bootstrap on
every pure-logic test would be slow and would couple crypto/const tests to HA
internals they have no reason to import.
"""
from __future__ import annotations

# Expose the pytest-homeassistant-custom-component plugin (hass fixture, HA
# event loop, custom-integration loader, socket blocking, etc.). Declaring it
# here only *registers* the fixtures; nothing starts Home Assistant until a
# test actually requests `hass`.
pytest_plugins = ["pytest_homeassistant_custom_component"]
