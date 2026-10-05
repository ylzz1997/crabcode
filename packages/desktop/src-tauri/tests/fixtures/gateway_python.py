"""Isolated Python/venv/pip fixture for Desktop's native startup tests.

The import probe executes as real Python and the CLI serves real HTTP. Only
environment creation and pip are simulated; tests never install into the host.
"""

import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import sys


CLI = '''
def entry():
    import json, os, sys, time
    from pathlib import Path
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from crabcode_gateway.server import run_server
    root = Path(__file__).parent.parent.parent
    config = json.loads((root / "fixture.json").read_text())
    (root / "started.pid").write_text(str(os.getpid()))
    if config["mode"] == "exit":
        raise RuntimeError("fixture startup failure")
    if config["mode"] == "hang":
        time.sleep(60)
    port = int(sys.argv[sys.argv.index("--port") + 1])
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok", "version": config["health_version"],
                               "protocol_version": config["health_protocol"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()

if __name__ == "__main__":
    entry()
'''


def setup(root, mode, version):
    modules = root / "modules"
    gateway = modules / "crabcode_gateway"
    cli = modules / "crabcode_cli"
    gateway.mkdir(parents=True, exist_ok=True)
    cli.mkdir(parents=True, exist_ok=True)
    (gateway / "__init__.py").write_text(f"__version__ = {version!r}\n")
    (gateway / "protocol.py").write_text(
        f"GATEWAY_MIN_PROTOCOL_VERSION = GATEWAY_MAX_PROTOCOL_VERSION = {2 if mode == 'protocol' else 1}\n"
    )
    (gateway / "server.py").write_text(
        "import missing_gateway_dependency\n" if mode == "dependency" else "def run_server(): pass\n"
    )
    (cli / "__init__.py").write_text("")
    playwright = modules / "playwright"
    playwright.mkdir(parents=True, exist_ok=True)
    (playwright / "__init__.py").write_text("")
    (cli / "__main__.py").write_text(
        "raise ImportError('missing CLI entry point')\n" if mode == "cli" else CLI
    )
    (root / "fixture.json").write_text(json.dumps({
        "mode": mode, "version": version,
        "health_version": "0.0.0" if mode == "health_version" else version,
        "health_protocol": 2 if mode == "health_protocol" else 1,
    }))
    launcher = root / "bin" / "python"
    launcher.parent.mkdir(exist_ok=True)
    launcher.write_text("#!/bin/sh\nexec " + " ".join(map(shlex.quote, [
        sys.executable, str(Path(__file__).resolve()), str(root)
    ])) + ' "$@"\n')
    launcher.chmod(0o755)
    # Avoid timestamp/size based stale bytecode after the simulated pip repair.
    for cache in modules.rglob("__pycache__"):
        shutil.rmtree(cache)


root = Path(sys.argv[1])
args = sys.argv[2:]
if args[0] == "setup":
    setup(root, args[1], args[2])
elif args == ["--version"]:
    print("Python " + sys.version.split()[0])
elif args[:2] == ["-m", "venv"]:
    (root / "created-venv").write_text(args[3])
    version = json.loads((root / "fixture.json").read_text())["version"]
    setup(Path(args[3]), "dependency", version)
elif args[:3] == ["-u", "-m", "pip"]:
    (root / "ran-pip").write_text(" ".join(args))
    setup(root, "healthy", args[-1].split("==")[1])
elif args[:4] == ["-u", "-m", "playwright", "install"]:
    (root / "ran-playwright").write_text(" ".join(args))
elif args[0] == "-c" and "playwright.chromium.executable_path" in args[1]:
    print("chromium")
else:
    sys.path.insert(0, str(root / "modules"))
    if args[0] == "-c":
        sys.argv = ["-c", *args[2:]]
        exec(args[1], {"__name__": "__main__"})
    elif args[:2] == ["-m", "crabcode_cli"]:
        sys.argv = ["crabcode_cli", *args[2:]]
        runpy.run_module("crabcode_cli", run_name="__main__")
    else:
        raise RuntimeError(f"Unexpected fixture command: {args}")
