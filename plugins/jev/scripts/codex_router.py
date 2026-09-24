#!/usr/bin/env python3
"""Install, remove or check the jev Codex router: the local proxy, Codex's config and a launchd agent.

  python3 codex_router.py install [--api-key] [--port N] [--no-launchd]
  python3 codex_router.py uninstall [--no-launchd]
  python3 codex_router.py status
"""
import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))
os.environ.setdefault("JEV_PLATFORM", "codex")

from jev import config  # noqa: E402

MARK = "# jev-router"
LABEL = "com.jev.codex-router"


def codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"


def plist_path() -> Path:
    return Path("~/Library/LaunchAgents/%s.plist" % LABEL).expanduser()


def _is_key(line, key):
    stripped = line.strip()
    return stripped.startswith(key) and stripped[len(key):].lstrip().startswith("=")


def set_top_level(text, key, value):
    """Set `key = value` above the first [table]. Returns (text, previous line or None)."""
    lines = text.splitlines(keepends=True)
    first_table = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    new = "%s = %s  %s\n" % (key, value, MARK)
    for i in range(first_table):
        if _is_key(lines[i], key):
            previous, lines[i] = lines[i].rstrip("\n"), new
            return "".join(lines), previous
    lines.insert(first_table, new)
    return "".join(lines), None


def set_in_table(text, table, key, value):
    """Set `key = value` inside [table], creating the table at the end if needed."""
    lines = text.splitlines(keepends=True)
    new = "%s = %s  %s\n" % (key, value, MARK)
    header = next((i for i, line in enumerate(lines) if line.strip() == "[%s]" % table), None)
    if header is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        return text + separator + "\n[%s]\n%s" % (table, new), None
    end = next((i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    for i in range(header + 1, end):
        if _is_key(lines[i], key):
            previous, lines[i] = lines[i].rstrip("\n"), new
            return "".join(lines), previous
    lines.insert(header + 1, new)
    return "".join(lines), None


def plist(port, app: Path) -> dict:
    log = config.home() / "log"
    return {"Label": LABEL, "RunAtLoad": True, "KeepAlive": True,
            "ProgramArguments": ["/usr/bin/python3", str(app / "router" / "proxy.py"), "--port", str(port)],
            "EnvironmentVariables": {"JEV_PLATFORM": "codex",
                                     "CODEX_HOME": str(Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser())},
            "StandardOutPath": str(log / "router.out"), "StandardErrorPath": str(log / "router.err")}


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def healthy(port) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/_jev/health" % port, timeout=2) as response:
            return response.read() == b"ok"
    except OSError:
        return False


def install(args):
    port = args.port or config.load_settings()["router_port"]
    app = config.home() / "app"
    for name in ("jev", "router"):
        shutil.copytree(str(PLUGIN / name), str(app / name), dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
    record_path = config.home() / "router-install.json"
    path = codex_config()
    text = path.read_text() if path.exists() else ""
    if MARK in text:  # reinstall: undo the previous edit first so its recorded values stay the originals
        text = restore(text, config.read_json(record_path, {}) or {})
    elif path.exists():
        shutil.copy2(str(path), str(path) + ".jev-backup")
    base = "http://127.0.0.1:%d/%s" % (port, "v1" if args.api_key else "backend-api/codex")
    text, previous_url = set_top_level(text, "openai_base_url", json.dumps(base))
    text, previous_compression = set_in_table(text, "features", "enable_request_compression", "false")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    config.write_json(record_path, {"openai_base_url": previous_url, "enable_request_compression": previous_compression,
                                    "port": port})
    if not args.no_launchd:
        (config.home() / "log").mkdir(parents=True, exist_ok=True)
        plist_path().parent.mkdir(parents=True, exist_ok=True)
        with open(plist_path(), "wb") as handle:
            plistlib.dump(plist(port, app), handle)
        launchctl("bootout", "gui/%d" % os.getuid(), str(plist_path()))
        launchctl("bootstrap", "gui/%d" % os.getuid(), str(plist_path()))
    print("Installed: Codex now talks to %s. Restart Codex apps to pick it up." % base)


def restore(text, record):
    out = []
    for line in text.splitlines(keepends=True):
        if MARK in line:
            previous = record.get(line.split("=")[0].strip())
            if previous:
                out.append(previous + "\n")
            continue
        out.append(line)
    return "".join(out)


def uninstall(args):
    record_path = config.home() / "router-install.json"
    path = codex_config()
    if path.exists():
        path.write_text(restore(path.read_text(), config.read_json(record_path, {}) or {}))
    if not args.no_launchd and plist_path().exists():
        launchctl("bootout", "gui/%d" % os.getuid(), str(plist_path()))
        plist_path().unlink()
    if record_path.exists():
        record_path.unlink()
    print("Uninstalled: Codex talks to OpenAI directly again. Restart Codex apps to pick it up.")


def status(args):
    record = config.read_json(config.home() / "router-install.json", {}) or {}
    port = record.get("port") or config.load_settings()["router_port"]
    installed = codex_config().exists() and MARK in codex_config().read_text()
    print("config: %s · proxy on :%d: %s" % ("installed" if installed else "not installed", port,
                                            "healthy" if healthy(port) else "not responding"))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "uninstall", "status"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--api-key", action="store_true", help="Codex signs in with an API key, not ChatGPT")
    parser.add_argument("--no-launchd", action="store_true")
    args = parser.parse_args(argv)
    {"install": install, "uninstall": uninstall, "status": status}[args.action](args)


if __name__ == "__main__":
    main()
