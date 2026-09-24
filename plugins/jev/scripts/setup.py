#!/usr/bin/env python3
"""One-time setup for the jev plugin: search venv, Jev key, connectivity check.

Usage: python3 setup.py [venv] [key [--env-file PATH]] [check]   (no arguments runs venv and check)
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

from jev import config, jevclient  # noqa: E402

GRAPHIFY = ("graphifyy @ https://github.com/Graphify-Labs/graphify/archive/"
            "20a20d30d8e7eef77675651f0199d87f913bd3e7.tar.gz")
DEPENDENCIES = [GRAPHIFY, "mcp>=1.12,<2", "tree-sitter-sql"]


def find_uv():
    for candidate in (shutil.which("uv"), "~/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv"):
        if candidate and os.access(os.path.expanduser(candidate), os.X_OK):
            return os.path.expanduser(candidate)
    sys.exit("uv not found. Install it from https://docs.astral.sh/uv/ and run setup again.")


def venv():
    target = config.home() / "venv"
    python = target / "bin" / "python"
    uv = find_uv()
    if not python.exists():
        subprocess.run([uv, "venv", "--python", "3.12", str(target)], check=True)
    subprocess.run([uv, "pip", "install", "--python", str(python), *DEPENDENCIES], check=True)
    subprocess.run([str(python), "-c", "import graphify, mcp, networkx"], check=True)
    print("Search environment ready: %s" % target)


def key(env_file):
    found = {}
    try:
        for line in Path(env_file).read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() in config.KEY_NAMES and value.strip():
                found[name.strip()] = value.strip().strip("'\"")
    except OSError:
        sys.exit("Could not read %s" % env_file)
    if not found:
        sys.exit("No TYPESAFE_API_KEY or OPENROUTER_API_KEY in %s" % env_file)
    answer = input("Copy %s from %s into %s? [y/N] "
                   % (", ".join(sorted(found)), env_file, config.home() / "credentials"))
    if answer.strip().lower() != "y":
        sys.exit("Nothing copied.")
    target = config.home() / "credentials"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if target.exists():
        for line in target.read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep:
                existing[name.strip()] = value.strip()
    existing.update(found)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("".join("%s=%s\n" % item for item in sorted(existing.items()) if item[0] in config.KEY_NAMES))
    os.chmod(str(target), 0o600)
    print("Stored %s (mode 600)." % target)


def check():
    answers = jevclient.ask({"text": "ping"}, {"q": {"type": "noul", "instructions": "The text says ping."}},
                            timeout=15)
    p = jevclient.noul(answers, "q")
    if p is None:
        sys.exit("Jev check failed: no key found, or the request did not succeed.")
    print("Jev reachable (p=%.2f)." % p)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("steps", nargs="*", choices=["venv", "key", "check"], default=["venv", "check"])
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    for step in args.steps:
        if step == "venv":
            venv()
        elif step == "key":
            key(args.env_file)
        else:
            check()


if __name__ == "__main__":
    main()
