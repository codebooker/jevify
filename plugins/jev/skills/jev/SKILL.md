---
name: jev
description: Jev token-saver controls. Use only when the user types /jev or $jev (status, on, off, gate, trim, skills, help).
---

# /jev

The jev plugin's prompt hook normally answers `/jev` commands before you see them. If you are reading this, the hook did not intercept the command. Run the plugin's command line with the user's arguments and show its output verbatim, then stop:

    python3 "<directory of this SKILL.md>/../../jev/hookmain.py" cli <arguments>

For example, `/jev gate on` becomes `... hookmain.py cli gate on`. Do nothing else.
