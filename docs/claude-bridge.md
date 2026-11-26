# Claude bridge: use Lumi from Claude Desktop, on your Claude plan

Claude Desktop can call Lumi as a tool. Claude does the thinking on your own
Claude subscription, and when it needs Lumi (her chat, her memories, her status)
it calls her on this PC. No Anthropic API key is used, no login is copied, and
the Claude app's window is never driven.

## What Claude gets

| Tool | What it does |
|---|---|
| `ask_lumi` | Sends a message to Lumi as you and returns her reply |
| `lumi_status` | Whether she's thinking or speaking |
| `lumi_recent_chat` | The last few chat messages |
| `lumi_search_memory` | Searches her memories of you |

## Setup

1. Start Lumi once, so the bridge token is created at
   `data\credentials\claude_bridge.token`. It's gitignored and only this PC's
   Lumi process and the MCP script read it.
2. Open Claude Desktop's config: **Settings → Developer → Edit Config**, or
   `%APPDATA%\Claude\claude_desktop_config.json`. Add this under `mcpServers`
   (adjust the path if your repo is elsewhere):

```json
{
  "mcpServers": {
    "lumi": {
      "command": "C:/Users/anguy/anaconda3/envs/lumi/python.exe",
      "args": ["C:/Users/anguy/OneDrive - Kennesaw State University/Documents/Lumi-master/scripts/lumi_mcp.py"]
    }
  }
}
```

3. Restart Claude Desktop. Lumi's four tools appear under the tools icon.

Claude Code can use the same script: `claude mcp add lumi -- C:/Users/anguy/anaconda3/envs/lumi/python.exe C:/Users/anguy/OneDrive - Kennesaw State University/Documents/Lumi-master/scripts/lumi_mcp.py`.

## Safety

- Only this PC can use it. The bridge refuses requests from the tunnel port, from
  the LAN, and from any request that carries a browser `Origin` header, so a web
  page open in a browser can't call it.
- Every request needs the bearer token. Regenerate it by deleting the token file
  and restarting Lumi; the MCP script then needs the new token, which it reads
  from the same file, so nothing else to edit.
- Chat commands (anything starting with `!`) are refused over the bridge.
- Bridge messages are marked `via: claude` in her history, and they never count as
  Minecraft orders.

## Limits

- Each `ask_lumi` waits up to 3 minutes for her reply, and she answers with her
  own model (local Ollama, or the cloud escalation you've set). Claude doesn't pay
  for her tokens; her tokens are her own models'.
- One bridge message runs at a time, so two questions queue up.
