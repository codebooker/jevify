"""Jevify v1: Graphify lexical candidates, Jev relevance, top-3 seeds, BFS depth 2 (from benchmarks/jevify_run.py)."""
from __future__ import annotations

import os
import re

from graphify import serve

from jev import jevclient

SEARCH_TOKENS, CONTEXT_TOKENS, CANDIDATES, SEEDS = 2400, 240, 32, 3
QUESTION = ("Is candidate {i} (id {id}) a relevant entry point for finding executable source evidence that "
            "answers the query? Judge the full query and source context, not just shared words. Do not answer "
            "the repository question; select relevant graph entry points.")


def cap(text: str, budget_tokens: int) -> str:
    limit = budget_tokens * 4
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def candidates(graph, root: str, query: str) -> list:
    found, sources = [], {}
    for _score, node_id in serve._score_nodes(graph, serve._query_terms(query)):
        data = graph.nodes[node_id]
        relative = data.get("source_file") or ""
        if not relative:
            continue
        if relative not in sources:
            try:
                with open(os.path.join(root, relative), errors="replace") as handle:
                    sources[relative] = handle.read().splitlines()
            except OSError:
                sources[relative] = None
        lines = sources[relative]
        if lines is None:
            continue
        match = re.search(r"\d+", str(data.get("source_location", "")))
        start = int(match.group()) if match else 1
        found.append({"id": node_id, "label": data.get("label", ""), "path": relative, "line": start,
                      "context": cap("\n".join(lines[max(0, start - 5):start + 17]), CONTEXT_TOKENS)})
        if len(found) == CANDIDATES:
            break
    return found


def search(graph, root: str, query: str, ask) -> str:
    found = candidates(graph, root, query)
    if not found:
        return "No matching graph candidates. Try rg with exact names."
    answers = ask({"query": query, "candidates": found},
                  {str(i): {"type": "noul", "instructions": QUESTION.format(i=i, id=c["id"])}
                   for i, c in enumerate(found)}, timeout=10)
    scores = [jevclient.noul(answers, str(i)) for i in range(len(found))]
    if answers is None or any(score is None for score in scores):
        order, note = list(range(len(found))), " (Jev unavailable: lexical order)"
    else:
        order, note = sorted(range(len(found)), key=lambda i: (-scores[i], i)), ""
    seeds = [found[i]["id"] for i in order[:SEEDS]]
    view = serve._traversal_view(graph)
    nodes, edges = serve._bfs(view, seeds, 2)
    text = "Selected graph entry points%s: %s\n" % (note, ", ".join(graph.nodes[n].get("label", n) for n in seeds))
    text += serve._subgraph_to_text(view, nodes, edges, SEARCH_TOKENS, seeds=seeds)
    return cap(text, SEARCH_TOKENS)
