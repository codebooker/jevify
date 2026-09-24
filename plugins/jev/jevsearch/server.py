"""jev-search MCP server (stdio). One tool: jev_search.

No `from __future__ import annotations` here: FastMCP finds the Context parameter from its real annotation.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))

from mcp.server.fastmcp import Context, FastMCP  # noqa: E402

from jev import config, jevclient, state  # noqa: E402
from jevsearch import index, pipeline  # noqa: E402

server = FastMCP("jev-search")


def _session_root(ctx):
    meta = getattr(getattr(ctx, "request_context", None), "meta", None)
    extra = {}
    if meta is not None:
        extra = dict(getattr(meta, "model_extra", None) or {})
        if hasattr(meta, "model_dump"):
            extra.update(meta.model_dump(exclude_none=True))
    turn = extra.get("x-codex-turn-metadata")
    if isinstance(turn, str):
        try:
            turn = json.loads(turn)
        except ValueError:
            turn = None
    session_id = turn.get("session_id") if isinstance(turn, dict) else None
    cwd = state.read(session_id)["cwd"] if session_id else ""
    return config.repo_root(cwd) if cwd else None


def _spawn_build(root: str) -> None:
    log_path = config.home() / "log" / "index.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log:
        subprocess.Popen([sys.executable, str(PLUGIN / "jevsearch" / "index.py"), "build", root],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


@server.tool()
def jev_search(query: str, root: str = "", ctx: Context = None) -> str:
    """Find where something is implemented in the current repository using its code graph (Graphify with Jev ranking).
    Returns up to ~2.4k tokens of graph entry points and related symbols with file:line locations; read exact line
    ranges afterwards. Use rg instead for exact strings."""
    settings = config.load_settings()
    if not (settings["enabled"] and settings["search"]):
        return "jev_search is turned off (/jev on enables it). Use rg."
    project = os.environ.get("JEV_PROJECT_DIR", "")
    if root:
        repo = config.repo_root(root)
    elif project and "${" not in project:
        repo = config.repo_root(project)
    else:
        repo = _session_root(ctx)
    if not repo:
        return "Could not tell which repository this session is in. Call jev_search again with root set to its path."
    graph_path = config.index_dir(repo) / "graph.json"
    if not graph_path.exists():
        if not index.is_building(repo):
            _spawn_build(repo)
        return ("Building the code graph for %s (first time; usually under a minute). Use rg for now and try "
                "jev_search again later." % repo)
    note = ""
    if not index.is_building(repo) and index.is_stale(repo):
        _spawn_build(repo)
        note = " (refreshing in the background; recent edits may be missing)"
    result = pipeline.search(index.load_graph(repo), repo, query, jevclient.ask)
    return "Repository: %s%s. Paths below are relative to it.\n%s" % (repo, note, result)


if __name__ == "__main__":
    server.run()
