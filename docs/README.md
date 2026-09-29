# Lumi Docs

The root `../README.md` is the project overview. Everything deeper lives in this folder. Pick the doc that matches your question.

## Doc index

| Doc | Covers |
|---|---|
| [architecture.md](architecture.md) | Turn flow, topology, key files |
| [configuration.md](configuration.md) | `.env`, `constants.py`, `config/lumi.yaml` |
| [development.md](development.md) | Dev setup, tests, lint, pre-commit |
| [models.md](models.md) | Ollama, providers, model manager |
| [discord-voice.md](discord-voice.md) | DAVE, VAD, diagnostics |
| [security.md](security.md) | Threat model, incident response |
| [roadmap.md](roadmap.md) | Phases and status |
| [known-limitations.md](known-limitations.md) | What does not work yet |
| [troubleshooting.md](troubleshooting.md) | Common failures and fixes |

<!-- VERIFY: this table follows the brief's recommended rows. Add or remove rows to match the actual .md files in docs/ (ls docs/*.md). -->

## Where to look if you want to...

- **Change the persona.** See the persona editor section in the root README, or `config/lumi.yaml` in [configuration.md](configuration.md).
- **Add a new model provider.** [models.md](models.md).
- **Understand what happens on a turn.** [architecture.md](architecture.md).
- **Contribute code.** [development.md](development.md).
- **Report a security issue.** [security.md](security.md).
- **Fix something that broke.** [troubleshooting.md](troubleshooting.md), then [known-limitations.md](known-limitations.md) to check whether it is a known gap.

## Conventions

- Code references use `<file>:<symbol>` form, for example `modules/prompter.py:run()`.
- File paths are relative to the workspace root.
- Command examples are PowerShell unless noted.
- Diagrams are Mermaid where possible.

## Adding a doc

New docs go in this folder with a `.md` extension, and each one needs a row in the index above. If a doc grows long, split it so each file covers one concept.

## See also

- `../README.md`: the project overview
- `../CONTRIBUTING.md`: for contributors, if present
- The per-folder READMEs for file-level maps: `../modules/README.md`, `../sdk/README.md`, `../web/README.md`, `../data/README.md`