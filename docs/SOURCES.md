# Source basis and verification scope

Checked during this response on 2026-09-19. No live Devin session was run.

## Method source supplied by the user

Matt Shumer, “How to Run a Gauntlet Loop,” user attachment `Pasted markdown(8).md`.
Public article: https://somethingbig.ai/gauntlet-loop

The conversation's supplied file supports:

- Lines 49–55: lead decomposition, builder + fresh critic per piece, comparison and repetition.
- Lines 99–113: inspectable artifacts/references and concrete executable measurement bars.
- Lines 115–125: lead, rather than the implementer, chooses the decomposition and parallelism.
- Lines 129–141: separate fresh critic; no builder history/explanation; actual artifact comparison.
- Lines 143–151: no arbitrary fixed final quality round; user can stop.
- Lines 153–161: progress view and actual evolving outputs.
- Lines 163–171: optional write-enabled smoothing, distinct from the core loop.
- Lines 192–198: per-piece build/judge/repeat; blind A/B when possible.

The original prompt is linked by that attachment at
https://github.com/mshumer/Claude-of-Duty/blob/main/prompt.md.
This implementation's method grounding is the supplied full article; the original
GitHub prompt and kamtS source were not newly fetched during this coding response.
No code was copied from kamtS.

## Devin official documentation freshly read for the adapter

1. Commands & Flags
   https://docs.devin.ai/cli/reference/commands
   `--print`, `--prompt-file`, `--config`, `--respect-workspace-trust`, model and
   permission arguments; `--resume`/`--continue` exist but this runner does not use them.
   The docs describe `/loop` as running a prompt then reviewing the diff; this runner
   does not assume it supplies the external workstream state machine.

2. Permissions
   https://docs.devin.ai/cli/reference/permissions
   `Read(...)`, `Write(...)`, tool-name restrictions, MCP denies, and autonomous
   sandbox behavior. Direct file-edit tools still require appropriate Write grants.

3. Sandbox
   https://docs.devin.ai/cli/sandbox
   OS sandbox writable/read-denied paths; missing prerequisites fail closed;
   Linux requires bwrap and socat; native Windows sandbox is not supported.

4. Configuration file
   https://docs.devin.ai/cli/reference/configuration/config-file
   Configuration structure and `read_config_from` options.

5. Subagents
   https://docs.devin.ai/cli/subagents
   Informational only: this package uses external independent CLI calls, not
   Devin's native subagent lifecycle API.

No undocumented JSON-output CLI flag, cloud cancellation API, token accounting API,
or equivalence between Devin models and Claude Code's ultracode is assumed.

## Implementation choices, not quoted Matt requirements

- Python supervisor, atomic JSON checkpoints and an append-only event log.
- Independent Git clones, controlled commits, and fast-forward integration.
- Re-evaluating the already established bars on one identical integrated commit.
- Randomized path labels, call nonces, evidence hashes, and explicit output contracts.
- Programmatic progress-page updates rather than relying on the lead to keep writing a page.
- Explicit user authorization for local execution and no default quality-round cap.

These are implementation details. They do not prove the semantic correctness of
an AI judgment or make the implementation an official Matt/Cognition release.
