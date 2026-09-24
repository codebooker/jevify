# Jev for Codex

Stretches a ChatGPT/Codex plan's usage limits. Jev is a fast classifier billed by TypeSafe or OpenRouter, outside your plan, at about $0.04 per million tokens. The plugin uses it to:

- **Search code** with a graph instead of grep-and-read (`jev_search`: Graphify + Jev ranking).
- **Gate tool calls**: skip re-reading unchanged files, dumping huge files, and repeating identical searches. Starts in shadow mode, where it only logs.
- **Trim output**: condense large command output to the lines that matter. The full output is saved to disk.
- **Route skills**: suggest the one installed skill that fits your request.
- **Meter usage**: `/jev` shows plan-window usage, this session's credits, and what the plugin saved.

## Setup

1. `python3 plugins/jev/scripts/setup.py venv` creates the search environment in `~/.codex/jev/venv` (Python 3.12, Graphify pinned to commit `20a20d30`, `mcp`).
2. `python3 plugins/jev/scripts/setup.py key --env-file /path/to/.env` copies `TYPESAFE_API_KEY` or `OPENROUTER_API_KEY` into `~/.codex/jev/credentials` (mode 600). It asks before copying. Exporting either variable in your shell profile also works, because hooks run through your login shell.
3. `python3 plugins/jev/scripts/setup.py check` confirms Jev answers.
4. Install the plugin: add an entry to `~/.agents/plugins/marketplace.json`, then run `codex plugin add jev@<marketplace>`. Enable it in the app and trust its hooks when asked.

## Commands

Type `/jev` in the desktop app, or `$jev` in the CLI. These never reach the model and cost no tokens.

| Command | Effect |
|---|---|
| `/jev` | Status, plan usage, credits, savings |
| `/jev on`, `/jev off` | Everything on or off |
| `/jev gate` | Gate report for the last 7 days |
| `/jev gate off\|shadow\|on` | Gate mode |
| `/jev trim on\|off` | Output trimmer |
| `/jev skills on\|off` | Skill router |

## How each part behaves

- **`jev_search`:** Graphify's lexical match finds 32 candidate symbols, Jev scores their relevance, and the top 3 seed a two-hop graph walk. It returns about 2.4k tokens with `file:line` locations. The graph is built locally with tree-sitter at session start (no LLM involved) and stored outside your repository. If Jev is unavailable, lexical order is used.
- **Gate (`PreToolUse` on shell commands):** it only looks at reads (`cat`, `sed -n`, `head`, `tail`, `nl`) and searches (`rg`, `grep`, `find`, `ls`). It never touches edits, tests, builds or git.
  - Rules: duplicate read of an unchanged file, whole-file read over 600 lines, identical search with nothing changed since, and Jev judging the command unnecessary (p ≥ 0.9).
  - If the model repeats a denied command, it is let through.
  - `/jev gate` flags any would-be block whose file the model later edited.
- **Trimmer (`PostToolUse` on shell commands):**
  - It applies to outputs over about 2k tokens, except source reads, diffs and JSON.
  - It always keeps the first 10 lines, the last 30, and error or failure lines.
  - Jev picks the other relevant chunks, up to a 1.5k-token budget.
  - The model is told the exit status isn't shown and where the full output is saved.
- **Skill router (`UserPromptSubmit`):** Jev picks from the exact skill list Codex shows the model. It adds a one-line hint when its confidence is at least 0.6 and the prompt doesn't already name a skill.

Every Jev call fails open: on an error or timeout, Codex behaves as if the plugin weren't there.

## Privacy

Jev receives your prompt text, recent command lines, chunks of large command output, and short source excerpts from search. Everything is sent to TypeSafe or OpenRouter. Graph building is local, and nothing is written to your repository. The local log stores hashes, not prompt text.

## Files

All state lives in `~/.codex/jev/`: `settings.json`, `credentials`, `sessions/`, `log/events.jsonl`, `index/`, `outputs/`, `venv/`. Uninstalling the plugin and deleting that folder removes everything.

## Tests

```sh
cd plugins/jev
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py'          # hooks, on Python 3.9
~/.codex/jev/venv/bin/python -m unittest discover -s tests -p 'test_*.py' # everything, including search and the MCP server
```

Hook outputs are validated against Codex's own hook schemas (`tests/fixtures/codex-hook-schemas`, from `openai/codex@75ec81c8`).

## Verified

- 2026-09-22: 66 tests pass. Plugin validator (`plugin-creator/scripts/validate_plugin.py`) passes.
- 2026-09-22: `jev_search` on the full Virtonyx repository (1,750 files, 19.7k graph nodes, 24 s build) put an annotated answer file in its output for 18/18 benchmark questions with live Jev, versus 15/18 with lexical order alone. Median search was 0.49 s and output about 2.2k tokens.
