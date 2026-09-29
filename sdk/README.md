# Lumi External SDK

External applications connect to Lumi over a local websocket, register
their own actions, push context, and receive invocations. Lumi treats
each registered action as a tool her LLM can call, exactly like her
built-in ones.

The protocol is modeled after
[Vedal's Neuro SDK](https://github.com/VedalAI/neuro-sdk) so third-party
developers find the message shapes familiar.

## Quick start

```bash
pip install websockets