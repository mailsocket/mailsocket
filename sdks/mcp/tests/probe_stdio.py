"""One-shot stdio handshake probe against the real `mailsocket-mcp` binary.

Sends MCP `initialize` + `tools/list` + `tools/call` over the server's stdio and
prints what comes back. It proves the wire protocol and the clean-error path:
with no real API key the SDK hits the network and the server must return a tidy
`isError` result (no traceback, no key). Not part of pytest — run manually.

    cd mcp && python tests/probe_stdio.py
"""
import json
import os
import shutil
import subprocess

BIN = shutil.which("mailsocket-mcp")


def main():
    if not BIN:
        raise SystemExit(
            "mailsocket-mcp not found on PATH — install it first, e.g. "
            "`pip install -e .` from mcp."
        )
    env = dict(os.environ)
    env["MAILSOCKET_API_KEY"] = "ms_live_test1234567890"  # fake key
    # Closed localhost port -> the SDK fails fast, deterministically, OFFLINE,
    # proving the server returns a tidy isError (no traceback, no key).
    env["MAILSOCKET_BASE_URL"] = "http://127.0.0.1:1/api/v1"
    proc = subprocess.Popen(
        [BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )

    def rpc(method, params, id_):
        proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params}) + "\n").encode()
        )
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                return None
            msg = json.loads(line)
            if msg.get("id") == id_:
                return msg

    init = rpc(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}},
        1,
    )
    print("initialize serverInfo.name:", init["result"]["serverInfo"]["name"])

    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n")
    proc.stdin.flush()

    tools = rpc("tools/list", {}, 2)
    print("tools/list names:", sorted(t["name"] for t in tools["result"]["tools"]))

    call = rpc("tools/call", {"name": "create_inbox", "arguments": {"label": "probe"}}, 3)
    res = call["result"]
    print("create_inbox isError:", res["isError"], "| text:", res["content"][0]["text"][:80])

    proc.stdin.close()
    proc.wait(timeout=10)
    print("server exit code:", proc.returncode)


if __name__ == "__main__":
    main()
