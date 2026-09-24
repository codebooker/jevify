---
name: lite
description: Hand a task to the low-cost Haiku helper. Type /jev:lite <task>.
argument-hint: "[task]"
disable-model-invocation: true
---

Delegate this task to the `jev:helper` subagent (Haiku) with a complete, self-contained brief (files, the exact change, how to verify), then report its result briefly. Trust its report; only re-check if it reports a problem.

Task: $ARGUMENTS
