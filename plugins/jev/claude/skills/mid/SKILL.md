---
name: mid
description: Hand a task to the lower-cost Sonnet worker. Type /jev:mid <task>.
argument-hint: "[task]"
disable-model-invocation: true
---

Delegate this task to the `jev:worker` subagent (Sonnet) with a complete, self-contained brief (files, the exact change, how to verify), then report its result briefly. Trust its report; only re-check if it reports a problem.

Task: $ARGUMENTS
