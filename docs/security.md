# Security & Privacy

Lumi is designed as a secure, local-first agentic companion. Because she possesses ambient perception capabilities and can execute local tools, strict operational boundaries are enforced to protect host system integrity and user privacy.

## Local-First Architecture

Lumi operates entirely within the local environment.

* **Data Locality:** All ambient screenshots (`data/logs/images/`), STT transcripts, and long-term embeddings (`data/memories/chroma.db`) are generated and stored exclusively on the host machine.
* **No Telemetry:** The system does not phone home, sync to cloud providers, or utilize external data brokerage services.

## Secrets Management

Credentials and authorization tokens are strictly segregated from the codebase to prevent accidental commits.

* **Core Configuration:** Discord bot tokens and environment variables are stored in the root `.env` file.
* **Third-Party APIs:** External service keys, such as `Fish_Audio_Key`, are isolated within `data/API/`.
* **Local Integrations:** Authentication for external local services, like VTube Studio, is managed via dedicated files (e.g., `data/vtubeStudio_token.txt`).
* **SDK Authentication:** Bearer tokens for external scripts utilizing the WebSocket SDK are securely generated and stored in `data/memories/sdk_token.txt`.

## Prompt Injection & Tool Execution Defenses

Because Lumi acts as an autonomous agent capable of executing tools (`modules/agent_tools.py`), she is inherently exposed to indirect prompt injection. A malicious instruction hidden within a Discord chat, a parsed webpage, or a read document could attempt to hijack her reasoning loop and execute unauthorized actions. Lumi mitigates this through a defense-in-depth architecture:

* **Least Privilege Authorization:** The agent's execution scope is heavily restricted. Lumi is only granted the minimum necessary permissions required to operate, bounding the blast radius if an injection succeeds.
* **Contextual Segregation:** To prevent data poisoning, `modules/prompter.py` maintains a strict separation between core system instructions and untrusted user inputs (such as Discord messages or visual context). The model is instructed to never treat retrieved text or external inputs as executable commands.
* **Action Gating:** High-stakes or irreversible actions triggered by the agent require human approval to execute. An injected command cannot autonomously bypass this deterministic check.

## Web Dashboard & SDK Authentication

Lumi exposes a localized web interface (`web/login.html`) and an SDK protocol (`sdk/server.py`).

* **Dashboard Access:** Interface access is gated by credentials stored in a local SQLite database (`data/memories/users.db`).
* **SDK Handshake:** Any external application or script attempting to interface with Lumi via `sdk/client.py` must authenticate using the token specified in `sdk_token.txt` before any actions are processed.