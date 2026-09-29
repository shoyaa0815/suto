"""Minimal local JSON-RPC MCP stdio fixture; no network or production server."""

import json
import os
import sys


def main():
    if secret := os.environ.get("SUTO_MCP_TEST_SECRET"):
        sys.stderr.write(secret + "\n")
    if malformed := os.environ.get("SUTO_MCP_TEST_BAD_STDOUT"):
        sys.stdout.write(malformed + "\n")
        sys.stdout.flush()
    pidfile = os.environ.get("SUTO_MCP_TEST_PIDFILE")
    if pidfile:
        with open(pidfile, "w", encoding="ascii") as output:
            output.write(str(os.getpid()))
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if "id" not in request:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "suto-test", "version": "1"},
            }
        elif method == "tools/list":
            schema = {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            }
            if os.environ.get("SUTO_MCP_TEST_BAD_SCHEMA"):
                schema = {"type": "array"}
            result = {"tools": [{
                "name": "echo", "description": "Echo",
                "inputSchema": schema,
            }]}
        elif method == "tools/call":
            result = {"content": [{
                "type": "text", "text": request["params"]["arguments"]["message"].upper(),
            }]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
