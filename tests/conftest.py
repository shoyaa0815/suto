"""Keep normal tests independent of host MCP commands and credentials."""

import pytest


@pytest.fixture(autouse=True)
def disable_host_mcp_configuration(monkeypatch):
    # An empty value also prevents main.py's load_dotenv() from reintroducing
    # a developer's local MCP path during tests. MCP tests opt in explicitly.
    monkeypatch.setenv("SUTO_MCP_CONFIG", "")
