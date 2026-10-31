---
name: sparkhub
description: Check or change which local model the DGX Spark (SparkHub) is serving - Fast (qwen36) or Quality (Flash). Use before starting long unattended work, when the user asks for the better or the faster model, or when a request fails with "not loaded" / "switching".
---

# SparkHub modes

The DGX Spark serves **one model at a time** behind SparkHub:

| Mode | Model | Speed | Use for |
|---|---|---|---|
| `fast` | qwen36 (Qwen3.6-35B-A3B) | ~100 tok/s alone, ~60 with 4 agents | interactive work: quick edits, questions, shell and sysadmin help, short tasks |
| `quality` | Flash (Qwen3.8-Flash-Next) | ~40 tok/s alone, ~22 with 4 agents | hand-off work: long agentic coding tasks, careful writing, anything unattended where quality matters more than speed |

Both have a 131k-token context window. Thinking is off by default; the `-thinking` model variants turn it on.

## Tools (MCP server `sparkhub`)

- `sparkhub_status`: the loaded mode, whether a switch is running, and how many other clients are active.
- `sparkhub_modes`: both modes, their settings and which is loaded.
- `sparkhub_switch_mode {mode, drain?, wait?}`: load the other model.

## Rules

1. **Switching costs 4-5 minutes** during which no model answers, and it interrupts anyone else using the Spark.
   Switch only when the task clearly needs the other mode, and never back and forth within a task.
2. **Ask the user before switching**, unless they already asked for that mode. Say why and that it takes ~5 minutes.
3. If the switch is refused because other clients are active, tell the user who is active. Offer `drain: true`
   (wait until they are quiet) rather than retrying in a loop. Never try to force it.
4. Keep `wait` at its default (true): the tool returns when the new model is ready and the conversation continues
   on it. Do not send model requests while a switch is running.
5. The model name `auto` always means "whatever is loaded", so the session keeps working after a switch.
   A request for `fast` or `quality` while the other is loaded fails with 409 "not loaded"; it does not switch.

## Without the MCP server

The `sparkhub` command does the same from a shell: `sparkhub status`, `sparkhub switch quality --drain`,
`sparkhub cancel`.
