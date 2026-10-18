# Lumi Docs

The root `../README.md` is the project overview. Everything deeper lives
in this folder. Pick the doc that matches your question.

## Doc index

| Doc | Covers |
|---|---|
| [architecture.md](architecture.md) | Turn flow, process and threads, module topology, prompt stack, tool dispatch, SDK bridge, external Minecraft processes, web server internals, diagnostics telemetry |
| [configuration.md](configuration.md) | `.env`, `constants.py`, `config/lumi.yaml`, reset procedures |
| [development.md](development.md) | Dev setup, module contract, web-server conventions, test suite, code conventions, adding providers, tools, and pages |
| [models.md](models.md) | Ollama, two-path routing, local and cloud routing, retries and rate limits, TTS/STT, embeddings, vision |
| [discord-voice.md](discord-voice.md) | DAVE receive, VAD, group awareness, diagnostics |
| [remote-access.md](remote-access.md) | Cloudflare Tunnel, remote vs host access tiers, Cloudflare Access sign-in, verify-before-sharing checklist |
| [security.md](security.md) | Threat model, Phase 9 guards, web client controls, incident response |
| [roadmap.md](roadmap.md) | All ten phases, changelog, status, and what remains |
| [known-limitations.md](known-limitations.md) | Architectural limitations, web client gaps, and behavior that is not a bug |
| [troubleshooting.md](troubleshooting.md) | Common failures, security refusals, web client, voice, memory, uploads |

## Where to look if you want to...

- **Change the persona.** See [configuration.md](configuration.md) for the file structure, or open the Persona page in the web client for a live form editor.
- **Add a new model provider.** [models.md](models.md) for how the provider layer works, then [development.md](development.md) for the walkthrough.
- **Understand what happens on a turn.** [architecture.md](architecture.md) has the full flow, prompt stack, and tool dispatch.
- **Contribute code.** [development.md](development.md) covers environment setup, the module contract, tests, lint, and adding new tools, providers, or pages.
- **Report a security issue.** [security.md](security.md) has the threat model, guard list, and incident response playbook.
- **Fix something that broke.** [troubleshooting.md](troubleshooting.md) first, then [known-limitations.md](known-limitations.md) to check whether it is a known gap.
- **Understand a config value.** [configuration.md](configuration.md) is the map. `constants.py` is the source of truth.
- **See what's shipped and what's left.** [roadmap.md](roadmap.md).
- **Decide what runs locally and what goes to the cloud.** [models.md](models.md#local-and-cloud-routing).
- **Work on the browser UI.** [`../web/README.md`](../web/README.md) for client conventions, [architecture.md](architecture.md#web-server-modulessocketiopy) for the server side.
- **Use Lumi from campus or share her with someone.** [remote-access.md](remote-access.md) covers the tunnel, what remote users can do, and Cloudflare Access.
- **Write a plugin.** [`../sdk/README.md`](../sdk/README.md) for the protocol, then [`../plugin/README.md`](../plugin/README.md) for the reference plugin.
- **Let Lumi play Minecraft.** [`../sdk/examples/minecraft/README.md`](../sdk/examples/minecraft/README.md) for the bot and its Voyager actions, [`../plugin/README.md`](../plugin/README.md) for the autonomous agent and the `:5002` dashboard. Read [security.md](security.md#minecraft-bot-control-api) first.

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
- Per-folder READMEs for file-level maps:
  - [`../modules/README.md`](../modules/README.md) — runtime modules
  - [`../web/README.md`](../web/README.md) — local web client
  - [`../sdk/README.md`](../sdk/README.md) — external SDK
  - [`../sdk/examples/minecraft/README.md`](../sdk/examples/minecraft/README.md) — Minecraft bot and Voyager integration
  - [`../plugin/README.md`](../plugin/README.md) — reference plugin, Minecraft agent and dashboard
  - [`../data/README.md`](../data/README.md) — runtime state
  - [`../tests/README.md`](../tests/README.md) — test suite and harnesses