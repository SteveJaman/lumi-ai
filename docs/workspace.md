# Workspace: Google Calendar, Gmail and KSU mail

Lumi can show your calendar and inbox in the web client (the **Workspace**
tab), summarize or draft replies to a thread ("Ask Lumi"), and, in agent
mode, use calendar and email tools. Everything runs on your PC. Sign-ins are
stored under `data/credentials/`, which is git-ignored and off limits to
Lumi's file tools.

You create your own Google (and optionally Microsoft) app registration. Lumi
ships no shared keys, so your mail is only ever reachable with credentials
you made.

- [Connect Google](#connect-google)
- [Connect KSU mail](#connect-ksu-mail)
- [Using it](#using-it)
- [What is stored, and what is protected](#what-is-stored-and-what-is-protected)
- [Troubleshooting](#troubleshooting)
- [For developers](#for-developers)

## Connect Google

One-time setup, about ten minutes.

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and
   create a project (any name).
2. **APIs & Services → Library**: enable the **Gmail API** and the
   **Google Calendar API**.
3. **APIs & Services → OAuth consent screen**: choose **External**, fill in
   the app name and your email, and add your own Google address under
   **Test users**.
4. **Publish the app** (consent screen → **Publish app**, status "In
   production"). It stays unverified, which is fine for your own use: Google
   shows an "unverified app" warning once. Skipping this step is the usual
   cause of weekly "sign-in expired" errors, because tokens for apps left in
   **Testing** expire after 7 days.
5. **APIs & Services → Credentials → Create credentials → OAuth client ID**,
   application type **Desktop app**. Download the JSON.
6. In Lumi: **Settings → Connections → Google**. Paste the JSON and press
   **Save client**, then **Connect Google**. A Google tab opens. Sign in and
   tick **every** permission (calendar, Gmail read/modify, send). The
   Connections page updates by itself when you're done.

Connect from the PC that runs Lumi, at `http://127.0.0.1:5000`. Lumi refuses
to start a Google sign-in through a tunnel or another hostname.

**If Google says `redirect_uri_mismatch`:** create the client as a
**Web application** instead, and add this exact authorized redirect URI:
`http://127.0.0.1:5000/api/workspace/google/callback`. Then paste that
client's JSON.

Permissions Lumi asks for: Calendar events, Gmail read and label changes
(read, mark read/unread, archive), Gmail send, and your email address. It
never asks to permanently delete mail.

## Connect KSU mail

> **Hidden by default.** KSU doesn't let students register apps in Microsoft
> Entra (creating one fails with a 403 "Insufficient privileges"), so the KSU
> card and the KSU inbox filter are switched off in the web client. The code
> still works. To bring them back, run
> `localStorage.setItem('lumi-ksu', '1')` in the browser console and reload.
> Option B below needs no Entra access.

KSU student mail is Microsoft 365. Pick whichever of these your university
allows.

### Option A: Microsoft 365 (Graph)

1. Open [Microsoft Entra](https://entra.microsoft.com/) → **App
   registrations → New registration**. Name it anything. Supported account
   types: **Accounts in any organizational directory**. Leave the redirect
   URI empty.
2. **Authentication → Advanced settings → Allow public client flows: Yes**.
3. **API permissions → Add → Microsoft Graph → Delegated**: `Mail.ReadWrite`,
   `Mail.Send`, `User.Read`.
4. Copy the **Application (client) ID**.
5. In Lumi: **Settings → Connections → KSU Student Mail → Microsoft 365**.
   Paste the ID (tenant: leave `organizations`) and press **Connect KSU**.
6. Lumi shows a code. Open the link it shows (microsoft.com/devicelogin),
   enter the code, and sign in with your KSU account.

Many universities block students from granting outside apps. If you see
"needs administrator approval" (or the sign-in fails with a message to that
effect), use Option B. No secret is stored for this flow; the app is a public
client.

### Option B: Gmail label fallback

This works without any approval from the university, but Lumi can only
*read* KSU mail this way (view, mark read, archive, summarize). Replying as
KSU isn't possible, because mail would be sent from your Gmail address.

1. In KSU mail (Outlook), set up forwarding of your inbox to your Gmail
   address.
2. In Gmail, create a label (for example `KSU`) and a filter that applies it
   to mail from your KSU address (or forwarded mail).
3. In Lumi: connect Google first, then **Settings → Connections → KSU Student
   Mail → Gmail label (fallback)**, type the label name and press **Use this
   label**.

Mail with that label shows under **KSU**; the **Gmail** view hides it so
nothing appears twice.

## Using it

**Workspace → Agenda.** The next 14 days, grouped by day. Click an event for
its details. **Add an event** creates it on your primary calendar, in your
browser's timezone.

**Workspace → Inbox.** Filter by All, Gmail or KSU. Open a thread to read it
as plain text (remote images and HTML are never loaded). **Mark read /
unread** and **Archive** act on the mailbox. **Ask Lumi** puts the thread in
the chat and asks her to summarize it or draft a reply. She shows you a
draft; nothing is sent.

**Speed.** Both views come from a local cache and open instantly. A stale
cache is refreshed in the background when you open the page (calendar after
5 minutes, mail after 2), or with **Refresh**. Nothing refreshes on a timer,
so an idle Lumi makes no network calls.

**Lumi's tools (agent mode).** When an account is connected she can use
`list_events`, `create_event`, `delete_event`, `list_emails`, `read_email`,
`send_email`, `mark_email_read` and `archive_email`. These need agent mode
(`!agent_on` or the chip in the chat). `send_email`, `delete_event` and
`create_event` ask you first, through the same approval flow as `run_command`
(the HITL bridge when a client is connected, the terminal otherwise). The
prompt shows who it goes to, the subject and the body, or which event will be
deleted. She is also limited to 10 emails per hour and 10 recipients per
email.

## What is stored, and what is protected

| What | Where | Notes |
|---|---|---|
| Google OAuth client | `data/credentials/google_client.json` | The file you downloaded |
| Google sign-in | `data/credentials/google_token.json` | Includes a refresh token; treat it like a password |
| Microsoft sign-in | `data/credentials/microsoft_token.json` | Refresh token for Graph |
| KSU settings | `data/credentials/ksu.json`, `microsoft.json` | Mode, label, client ID. No secrets |
| Cache | `data/workspace/cache.json` | Message summaries (sender, subject, snippet) and events; not message bodies |

- Both folders are git-ignored, and Lumi's `read_file`, `write_file`,
  `list_dir` and `search_files` refuse them. `run_command` refuses commands
  that name them, but that check is a speed bump, not a wall (a shell can
  reach anything); the approval prompt shows you the command.
- Every `/api/workspace/*` route is host-only. A tunnel visitor gets 403.
- **Email and invites are written by strangers.** Anything Lumi reads from
  them reaches her inside `[FROM EMAIL]` / `[FROM CALENDAR]` markers, with
  look-alike markers defused, and her rules say text inside markers is data,
  never instructions. "Ask Lumi" messages are never treated as typed by you,
  so an email can't give her orders as if it were you. This reduces prompt
  injection; it can't remove it, which is why sending and deleting always ask.
- **Where your mail goes.** "Ask Lumi" and the email tools send message text
  to your chat model. If that is a cloud provider (Settings → Models), the
  text leaves your PC. With a local Ollama model it doesn't.
- **The chat remembers.** An "Ask Lumi" request becomes part of the chat
  history, which Lumi saves, and her memory module may extract facts from it.
  Wipe it from **Settings → Advanced** if you don't want that.
- **Disconnect** revokes the Google token with Google and deletes the local
  token and cache.

## Troubleshooting

| You see | Cause | Fix |
|---|---|---|
| "Enable the Gmail API and the Google Calendar API…" | An API isn't enabled in your Cloud project | Step 2 above |
| "Google sign-in expired" after about a week | App left in Testing | Publish the app (step 4), then reconnect |
| `redirect_uri_mismatch` | Desktop client rejected the redirect | Use a Web application client (see above) |
| "Some permissions weren't granted" | A box was unticked on Google's screen | Disconnect, connect again, tick everything |
| "needs administrator approval" (KSU) | The university blocks outside apps | Use the Gmail label fallback |
| "…isn't set up as a public client" | Public client flows are off | Entra → Authentication → Allow public client flows |
| "There's no Gmail label called…" | Label name doesn't match | Use the exact name from Gmail |
| Calendar or Mail show "Connect…" | Nothing connected, or sign-in expired | Settings → Connections |

## For developers

The code is `modules/workspace_hub.py` (accounts, providers, cache, tools,
snapshot) and `modules/workspace_routes.py` (the `/api/workspace/*` routes).
The page is `web/workspace.js`. Tests: `tests/test_workspace_*.py` (no
network needed; providers are faked).

Mail sources implement the small `MailProvider` interface (`list_recent`,
`get_message`, `set_read`, `archive`, `send`), so another provider means one
class and one branch in `WorkspaceHub._provider`.

`WorkspaceHub.get_compressed_snapshot()` is built from the cache only (no
network) and is also served at `GET /api/workspace/snapshot`. It is small
enough to push over a socket:

```json
{
  "v": 1,
  "next_event": {"title": "Statistics lecture", "start": "2026-11-14T15:00:00-05:00",
                 "all_day": false, "location": "Science Building 212", "in_min": 94},
  "unread_count": 3,
  "unread": [{"from": "KSU Registrar", "subject": "Spring registration opens Monday",
              "provider": "ksu", "ts": 1763135880}],
  "connected": {"google": true, "ksu": true}
}
```

`add_listener(fn)` calls `fn(snapshot)` whenever the snapshot changes (the
per-minute `in_min` countdown doesn't count as a change), from whichever
thread refreshed the cache. Lumi Island subscribes (through
`modules/island_bridge.py`) and shows it on its Home tab; its Calendar tab
reads ranges through `get_events_range()`. See
[architecture.md](architecture.md#lumi-island-bridge).
