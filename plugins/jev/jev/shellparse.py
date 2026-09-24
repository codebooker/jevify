"""Classify shell commands as file reads, searches, or other (never touched)."""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Optional

SEARCH_TOOLS = {"rg", "grep", "egrep", "fgrep", "ag", "find", "fd", "ls", "tree"}
UNSAFE = re.compile(r"[;&<>`]|\$\(|\|\|")
SHELLS = {"bash", "zsh", "sh"}


@dataclass
class Intent:
    kind: str                    # "read" | "search" | "other"
    path: Optional[str] = None   # absolute path, reads only
    start: Optional[int] = None  # 1-based; negative means the last N lines; None means line 1
    end: Optional[int] = None    # inclusive; None means end of file
    key: str = ""                # cwd + normalized command, for duplicate detection
    tool: str = "Bash"           # the tool that produced this intent (Bash, Read, Grep, Glob)


OTHER = Intent("other")


def command_of(event) -> object:
    tool_input = event.get("tool_input") or {}
    return tool_input.get("command") if isinstance(tool_input, dict) else None


def command_text(command) -> str:
    if isinstance(command, list):
        return " ".join(shlex.quote(str(part)) for part in command)
    return command if isinstance(command, str) else ""


def split_commands(text: str) -> list:
    """Split a shell line on top-level &&, ||, ; and newlines, leaving quoted text alone."""
    parts, current, quote, i = [], [], "", 0
    while i < len(text):
        char = text[i]
        if quote:
            current.append(char)
            quote = "" if char == quote else quote
            i += 1
        elif char in "'\"":
            quote, i = char, i + 1
            current.append(char)
        elif char == "\\" and i + 1 < len(text):
            current.append(text[i:i + 2])
            i += 2
        elif text[i:i + 2] in ("&&", "||"):
            parts.append("".join(current))
            current, i = [], i + 2
        elif char in ";\n":
            parts.append("".join(current))
            current, i = [], i + 1
        else:
            current.append(char)
            i += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def outside_quotes(text: str) -> str:
    """The text with quoted spans blanked, so a separator inside a search pattern is not mistaken for shell syntax."""
    out, quote, i = [], "", 0
    while i < len(text):
        char = text[i]
        if quote:
            out.append(" ")
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
            out.append(" ")
        else:
            out.append(char)
        i += 1
    return "".join(out)


def split_pipeline(text: str) -> list:
    """Split on top-level pipes only: an alternation inside a search pattern ('a|b') is not a pipe."""
    parts, current, quote = [], [], ""
    for char in text:
        if quote:
            current.append(char)
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char == "|":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def parse_all(command, cwd: str) -> list:
    """Every intent in one call. Codex batches reads ("sed ... && sed ... && rg ..."), so a call is a list."""
    segments = split_commands(_unwrap(command_text(command).strip()))
    return [parse(segment, cwd) for segment in segments] if segments else []


def parse(command, cwd: str) -> Intent:
    text = _unwrap(command_text(command).strip())
    if not text or "\n" in text or UNSAFE.search(outside_quotes(text)):
        return OTHER
    try:
        stages = [shlex.split(stage) for stage in split_pipeline(text)]
    except ValueError:
        return OTHER
    if not all(stages) or len(stages) > 2:
        return OTHER
    key = cwd + "\0" + " | ".join(" ".join(stage) for stage in stages)
    if len(stages) == 1:
        return _single(stages[0], cwd, key)
    first = _single(stages[0], cwd, key)
    if first.kind == "search":
        return first
    if first.kind != "read" or first.start is not None:
        return OTHER
    tool = os.path.basename(stages[1][0])
    if tool == "sed":
        return _sed(stages[1][1:], first.path, cwd, key)
    if tool == "head":
        count, files = _count(stages[1][1:])
        if count is None or files or count.startswith("+"):
            return OTHER
        return Intent("read", first.path, 1, int(count), key)
    return OTHER


def _unwrap(text: str) -> str:
    try:
        parts = shlex.split(text)
    except ValueError:
        return text
    if len(parts) == 3 and os.path.basename(parts[0]) in SHELLS and parts[1] in ("-c", "-lc"):
        return parts[2].strip()
    return text


def _abs(path: str, cwd: str) -> str:
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))


def _files(args):
    return [a for a in args if not a.startswith("-")], [a for a in args if a.startswith("-")]


def _single(argv, cwd, key) -> Intent:
    tool = os.path.basename(argv[0])
    if tool in SEARCH_TOOLS or argv[:2] in (["git", "grep"], ["git", "ls-files"]):
        return Intent("search", key=key)
    if tool in ("cat", "nl"):
        files, flags = _files(argv[1:])
        allowed = {"-n"} if tool == "cat" else {"-ba", "-b", "-a"}
        if len(files) == 1 and all(flag in allowed for flag in flags):
            return Intent("read", _abs(files[0], cwd), None, None, key)
        return OTHER
    if tool in ("head", "tail"):
        count, files = _count(argv[1:])
        if count is None or len(files) != 1:
            return OTHER
        if tool == "head":
            return OTHER if count.startswith("+") else Intent("read", _abs(files[0], cwd), 1, int(count), key)
        if count.startswith("+"):
            return Intent("read", _abs(files[0], cwd), int(count[1:]), None, key)
        return Intent("read", _abs(files[0], cwd), -int(count), None, key)
    if tool == "sed":
        return _sed(argv[1:], None, cwd, key)
    return OTHER


def _count(args):
    count, files, i = "10", [], 0
    while i < len(args):
        arg = args[i]
        if arg == "-n" and i + 1 < len(args):
            count, i = args[i + 1], i + 2
            continue
        if arg.startswith("-n"):
            count = arg[2:]
        elif arg.startswith("--lines="):
            count = arg[len("--lines="):]
        elif re.fullmatch(r"-\d+", arg):
            count = arg[1:]
        elif arg.startswith("-"):
            return None, []
        else:
            files.append(arg)
        i += 1
    return (count, files) if re.fullmatch(r"\+?\d+", count) else (None, [])


def _sed(args, source, cwd, key) -> Intent:
    if "-n" not in args:
        return OTHER
    rest = [a for a in args if a != "-n"]
    if not rest or any(a.startswith("-") for a in rest):
        return OTHER
    match = re.fullmatch(r"(\d+)(?:,(\d+|\$))?p", rest[0])
    if not match:
        return OTHER
    files = rest[1:]
    if source is None:
        if len(files) != 1:
            return OTHER
        source = _abs(files[0], cwd)
    elif files:
        return OTHER
    start = int(match.group(1))
    if match.group(2) is None:
        end = start
    elif match.group(2) == "$":
        end = None
    else:
        end = int(match.group(2))
    return Intent("read", source, start, end, key)
