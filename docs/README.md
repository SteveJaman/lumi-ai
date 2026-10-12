# Lumi Docs

The root `../README.md` is the project overview. Everything deeper lives
in this folder. Pick the doc that matches your question.

## Doc index

| Doc | Covers |
|---|---|
| [architecture.md](architecture.md) | Turn flow, module topology, prompt stack, tool dispatch, SDK bridge |
| [configuration.md](configuration.md) | `.env`, `constants.py`, `config/lumi.yaml`, reset procedures |
| [development.md](development.md) | Dev setup, module contract, test suite, code conventions, adding providers and tools |
| [models.md](models.md) | Ollama, two-path routing, cloud providers, TTS/STT, embeddings, vision |
| [discord-voice.md](discord-voice.md) | DAVE receive, VAD, group awareness, diagnostics |
| [security.md](security.md) | Threat model, Phase 9 guards, incident response |
| [roadmap.md](roadmap.md) | All ten phases, status, and what remains |
| [known-limitations.md](known-limitations.md) | Architectural limitations and behavior that is not a bug |
| [troubleshooting.md](troubleshooting.md) | Common failures, security refusals, voice, memory, uploads |

## Where to look if you want to...

- **Change the persona.** See [configuration.md](configuration.md) for the file structure, or open the Persona page in the web client for a live form editor.
- **Add a new model provider.** [models.md](models.md) for how the provider layer works, then [development.md](development.md) for the walkthrough.
- **Understand what happens on a turn.** [architecture.md](architecture.md) has the full flow, prompt stack, and tool dispatch.
- **Contribute code.** [development.md](development.md) covers environment setup, the module contract, tests, lint, and adding new tools, providers, or pages.
- **Report a security issue.** [security.md](security.md) has the threat model, guard list, and incident response playbook.
- **Fix something that broke.** [troubleshooting.md](troubleshooting.md) first, then [known-limitations.md](known-limitations.md) to check whether it is a known gap.
- **Understand a config value.** [configuration.md](configuration.md) is the map. `constants.py` is the source of truth.
- **See what's shipped and what's left.** [roadmap.md](roadmap.md).

## Conventions

- Code references use `<file>:<symbol>` form, for example `modules/prompter.py:run()`.
- File paths are relative to the workspace root.
- Command examples are PowerShell unless noted.
- Diagrams are ASCII, not Mermaid, so they render in any markdown viewer without a JavaScript dependency.

## Adding a doc

New docs go in this folder with a `.md` extension, and each one needs a
row in the index above. If a doc grows long, split it so each file
covers one concept.

Update the index row whenever the doc's scope changes. It is the first
thing a new reader sees and the last thing anyone remembers to fix.

## Related docs

- `../README.md`: the project overview
- `../CONTRIBUTING.md`: for contributors, if present
- Per-folder READMEs for file-level maps:
  - [`../modules/README.md`](../modules/README.md) — runtime modules
  - [`../web/README.md`](../web/README.md) — local web client
  - [`../sdk/README.md`](../sdk/README.md) — external SDK
  - [`../data/README.md`](../data/README.md) — runtime state
  - [`../tests/README.md`](../tests/README.md) — test suite and harnesses