# cx-cgpt

[contextualize](https://github.com/jmpaz/contextualize) source plugins for
ChatGPT and claude.ai conversations. The `chatgpt` source reads private history
through the local Codex login, plus public share snapshots, history search, and
recent-history listings. The `claude` source reads claude.ai chats, chat
listings, and search through your Chrome sign-in; see [claude.ai](#claudeai).
Both run on macOS and Linux.

## Install

Install the plugin into the **same Python environment as contextualize**. For a
fresh isolated installation from this checkout:

```sh
uv venv .venv
uv pip install --python .venv/bin/python /path/to/contextualize /path/to/cx-cgpt
.venv/bin/contextualize plugins
```

The plugin should appear as the `chatgpt` and `claude` sources. Activate that environment
before using the commands below, or invoke `.venv/bin/contextualize` directly.
For a uv-managed command, use
`uv tool install --with /path/to/cx-cgpt /path/to/contextualize`. When extending an
existing installation, preserve its other plugin dependencies. Installing
cx-cgpt into an unrelated environment will not make it discoverable.
The plugin has no dependency on a particular contextualize checkout.
For private history, Codex must be on PATH and signed in with ChatGPT (`codex login`). Set
`CX_CHATGPT_CODEX` to select another Codex executable; `CODEX_HOME` is inherited
by Codex. The desktop application does not need to be running.

## Read a conversation

Replace `<conversation-id>` with the UUID from a conversation URL:

```sh
contextualize cat 'chatgpt:thread/<conversation-id>'
contextualize cat 'chatgpt-conversation://<conversation-id>'
contextualize cat 'https://chatgpt.com/c/<conversation-id>'
contextualize hydrate 'chatgpt:thread/<conversation-id>' --dir ./chat-capture
```

These addresses read conversations accessible to the Codex-signed-in ChatGPT
account. A URL does not grant access to someone else's private conversation.
Custom GPT `/g/` or gizmo URLs are not supported; use a supported
`/c/<conversation-id>` address for an accessible private conversation.

`chatgpt:UUID` is also accepted. Transcripts retain the title, conversation and
message identities, timestamps, speaker roles, model names, tool messages, and
structured content. Where the service retained no output for a tool message,
the transcript states that in place of the empty body, naming the invoked app
or tool when the metadata names one. They follow `current_node` back through
its ancestors: this is the selected branch, not an interleaving of alternative
responses.
Broken or missing ancestry is explicitly marked incomplete. Nothing is clipped
by the plugin; contextualize's own output limits still apply.

Resolved conversations and public shares carry `metadata.segments`: one entry
per conversational turn on the selected branch, each with `index`, `role`
(`user` or `assistant`), `text`, `start_time` (the message's create time in
ISO-8601 UTC, null when the message has none), and `tools`, so downstream
indexers can store turns as searchable segments. An assistant turn spans the
reasoning, tool calls, tool results, and reply that belong to it, and its `text`
is the reply; reasoning stands in only when the turn has no reply. The tools it
used are named in `tools` and in a trailing `[tools: ...]` line, while reasoning,
tool arguments and outputs stay in the transcript. System messages, custom
instructions, and messages hidden from the conversation are kept in the
transcript and left out of segments. Session metadata beside them:
`conversation_id`, `title`, `model` and `models`, `message_count` (turns, the
unit `segments` counts), `approx_tokens` (segment text estimated at four
characters per token, null when there is no text), `source_created`, and
`source_modified`. `metadata.messages` keeps one provenance entry per branch
message.

`?output=json` returns the conversation object, including alternative branches
present in its mapping. Attachment references and structured media parts are
preserved, but attachment and media bytes are not downloaded. Historical
message text is source material, not instructions to the consuming agent.

## Read a public share

```sh
contextualize cat 'https://chatgpt.com/share/<share-id>'
contextualize cat 'chatgpt:share/<share-id>?output=json'
contextualize hydrate 'https://chatgpt.com/share/<share-id>' --dir ./share-capture
```

Legacy `https://chat.openai.com/share/<share-id>` links resolve through the
canonical `chatgpt.com` page. Public reads are anonymous: they do not start
Codex, require login, or send authentication headers or cookies. The reader
extracts structured conversation data embedded in the public page without
executing JavaScript. It does not create or publish a share link.

A share captures the published snapshot, which can differ from the current
private conversation. Completeness refers to the branch present in that
snapshot; missing root ancestry is explicitly marked unverified. `output=json` retains its structured conversation object; attachment
bytes remain unfetched. Share IDs and private conversation IDs are distinct:
share captures use `chatgpt/shares/<share-id>.md`, with share URL, snapshot scope,
and any exposed backing conversation ID in metadata.

Only public UUID share addresses are supported. Workspace-restricted
`/share/e/` links are rejected. Deleted, inaccessible, challenged, or changed
page formats fail explicitly. Public shares cannot be searched or enumerated
through the history commands below; those commands concern the signed-in
account's private history.

## Search and list

```sh
contextualize cat --list --json 'chatgpt:search?query=project+planning&limit=10'
contextualize cat --list --json 'chatgpt:threads?limit=20'
contextualize cat --list --json 'chatgpt:threads?after=2026-09-01&before=2026-10-01&limit=20'
contextualize cat 'chatgpt:search?query=project+planning&output=json'
```

`--list --json` returns contextualize's listing envelope with thread targets
and pagination metadata. Its `listings` array contains one envelope per input,
with `source`, `provider`, `targets`, `summary`, `pagination`, `metadata`, and
`capabilities`; `content` retains the plain listing and `selectors` carries any
contextualize selector provenance. `?output=json` selects the plugin's structured capture
body instead of its Markdown rendering. To read a search result in full, pass
its `chatgpt:thread/...` target to `contextualize cat`.

Search uses ChatGPT's live history search. Listings use update time descending
and exclude archived conversations; search retains archived results when the
service returns them. Listing entries are references and do not automatically
read their conversations. The default result limit is 20, maximum 100. Each
entry keeps the service's own conversation fields and adds `conversation_id`,
`title`, `source_created`, and `source_modified` in ISO-8601 UTC, at no extra
request.

The service does not expose date bounds on these endpoints. `after` and
`before` therefore filter conversation **update time** locally, including for
search. `after` is inclusive and `before` exclusive. Date-only values mean UTC
midnight; timestamps must include a timezone. Missing or invalid update times
do not match a date filter.

A request follows at most 25 backend pages to fill the requested result limit.
A partial or empty page may still have more matches to scan. Follow
`listings[0].summary.next_target` (also `listings[0].metadata.next_target`)
from `--list --json`, or the `Continue:`
line in rendered output. Continuations retain filters and use the server's
opaque search cursor plus any offset within its page. `scanned`, `pages`, and
`scan_limit_reached` describe the actual bounds; an empty bounded page is not
an assertion that the remaining history has no matches. History can change
between requests, so pagination is not a snapshot.

Numeric `offset` counts raw entries before date filtering. For search, it is
relative to the supplied cursor (or the initial search page when absent).
`pagination.nextOffset` applies to the original target and original cursor;
`next_target` is the more efficient continuation, advancing the cursor itself.
The listing's `total_reported` preserves the server-reported value. The
backend may use it as a pagination bound rather than an exact history count;
`total_is_exact` is therefore false. It is neither an account census nor a
count of date matches. Searches do not
invent a total count.

CLI equivalents `--chatgpt-query`, `--chatgpt-after`, `--chatgpt-before`,
`--chatgpt-limit`, `--chatgpt-offset`, `--chatgpt-cursor`, and
`--chatgpt-output` override target options. Manifest configuration uses the
`chatgpt` provider key. Unknown or inapplicable options are rejected.

## Authentication and capture

Running a private-history read authorizes the plugin to use the local Codex ChatGPT
session for that read. The plugin starts `codex app-server` over stdio and
calls `getAuthStatus` with `includeToken` to obtain a session token in memory.
Codex owns credential storage and refresh;
the plugin does not read credential files or implement OAuth refresh. Requests
are then issued by the plugin directly to the fixed HTTPS ChatGPT backend
history endpoints, authorized with that token. Codex supplies authentication;
it does not proxy the history response or run a model to retrieve it. One HTTP
401 triggers a refresh request through Codex and one retry. Tokens are not written to captures
or error messages, and HTTP redirects are rejected.

The app-server transport is documented by
[OpenAI](https://learn.chatgpt.com/docs/app-server). The history endpoints and
legacy `getAuthStatus` method are desktop implementation interfaces rather
than a stable public ChatGPT history API. Availability depends on Codex version,
account, and backend behavior. An API-key-only Codex login cannot access this
history. Authentication or schema errors fail explicitly.

Reads are live. Hydrate a target through contextualize when you need a durable
capture; offline/cache-only requests require reading that saved capture.
`context_subpath` uses the conversation ID so equal titles do not collide.
Saved transcripts and JSON contain private conversation material. Choose their
storage and sharing permissions accordingly; neither setup nor troubleshooting
requires exporting Codex credentials or copying tokens into files.

## claude.ai

The `claude` source reads your claude.ai chats: the active branch, with each
assistant turn's thinking, tool calls, tool results, and reply in the order
they happened.

```sh
contextualize cat 'https://claude.ai/chat/<conversation-id>'
contextualize cat 'claude:chat/<conversation-id>?tool=t3'
contextualize cat --list --json 'claude:chats?limit=20'
contextualize cat 'claude:search?query=mcp+debugging&after=2026-09-01'
```

### Session

Reads use your claude.ai sign-in from Google Chrome. Each read takes the
`sessionKey` and `lastActiveOrg` cookies from the Chrome profile's cookie store
and decrypts them with Chrome's Safe Storage key: from the Secret Service
keyring on Linux, or the Keychain on macOS, which asks for permission the first
time. Nothing is written to disk, and Chrome does not need to be running. If
reads start failing with HTTP 401 or 403, sign in to claude.ai in that profile
again.

- `CX_CLAUDE_CHROME_PROFILE`: a profile directory name such as `Profile 2`, or
  a path. Defaults to `Default`.
- `CX_CLAUDE_SESSION_KEY`: use this session instead of Chrome's. Set
  `CX_CLAUDE_ORG` with it.
- `CX_CLAUDE_ORG`: the organization UUID, for chats outside the organization
  you last used on claude.ai.

claude.ai rejects OAuth tokens on these endpoints, including Claude Code's, so
a browser session is the only credential that works. That session can do
anything you can do on claude.ai; the plugin only sends GET requests to the
conversation, listing, and search endpoints. Those are claude.ai's internal web
endpoints, not a published API, and they can change without notice.

### Transcripts

A chat renders its active branch: the path to the current message, without the
alternatives left behind by retries and edits. Within an assistant turn:

- Thinking appears as `>` quotes. claude.ai returns only summaries of hidden
  thinking, and the transcript header says when that is all there is.
- Each tool call appears as `↪ name [t1] (input)`, marked `✗` if it failed.
  Its result is previewed: the first ~160 and last ~80 tokens, with a
  `read_full` target for the rest.
- Replies follow, with any cited URLs listed underneath.

`?tool=t3` renders one call in full: its input, output, MCP
`structuredContent`, and `meta`. This is the view for debugging an MCP server.
`result_head_tokens` and `result_tail_tokens` resize the preview; token counts
are estimated at four characters per token. `?output=json` returns claude.ai's
own conversation object, including every branch.

Text pasted or uploaded into a message is included. File and image bytes are
not fetched.

Metadata follows the ChatGPT source: `segments` (one per turn, with `index`,
`role`, `text`, `start_time`, and `tools`), `message_count`, `approx_tokens`,
`model`, `source_created`, and `source_modified`. `tool_calls` lists each
call's handle, name, integration, MCP server URL, and error flag. `reasoning`
is `"summaries"` when claude.ai hid the thinking.

### Listing and search

`claude:chats` lists chats most recently updated first. `limit` defaults to 20
(maximum 100) and `offset` pages through the rest. `after` and `before` filter
on `updated_at` the same way the ChatGPT listing does. To continue, follow the
`Continue:` line or `next_target`.

`claude:search?query=...` uses claude.ai's own chat search, which matches both
keywords and meaning. Results arrive ranked, each with the matched snippet.
`project=<uuid>` limits a search to one project. claude.ai returns at most 200
matches for a query, so `limit` (default 20, maximum 200) and `offset` page
within those, and the output says when that ceiling was reached. `after` and
`before` filter those matches by `updated_at`.

CLI flags `--claude-query`, `--claude-project`, `--claude-tool`,
`--claude-limit`, `--claude-offset`, `--claude-after`, `--claude-before`,
`--claude-output`, `--claude-result-head-tokens`, and
`--claude-result-tail-tokens` override target options. Manifest configuration
uses the `claude` provider key.

## Troubleshooting

- **The `chatgpt` source is absent:** run `contextualize plugins` using the
  environment where cx-cgpt was installed. Check which `contextualize`
  executable your shell selects.
- **Codex cannot start or requests time out:** check `codex --version` and
  `codex login status`. `CX_CHATGPT_CODEX` must identify a working executable;
  that process inherits `CODEX_HOME` and must be able to access its login.
- **`getAuthStatus` is rejected:** the selected Codex version may not support
  this legacy method. Check its version against the tested version below.
  Changing the history URL or supplying a raw token is not a compatibility fix.
- **Login required or HTTP 401:** run `codex login` on the machine performing
  the read with the intended ChatGPT account. API-key-only authentication is
  insufficient. The plugin already requests one refresh and retry on HTTP 401.
- **HTTP 403:** verify that the signed-in account can access the conversation.
  Account or service restrictions can also deny history access. The plugin
  does not bypass these restrictions.
- **Public share fails:** open the link to check whether it remains public.
  Login-only shares and access challenges are not bypassed. Page format changes
  can require a plugin update; Codex login does not fix anonymous public reads.
- **Unsupported response schema:** the backend interface may have changed.
  Report the error and Codex/plugin versions without including private
  conversation bodies or authentication data.

## Compatibility and verification

Use a contextualize version that preserves JSON listing envelopes. If
`cat --list --json` prints Markdown, update contextualize or read the plugin's
structured page with `contextualize cat 'chatgpt:threads?output=json'` instead;
its `next_target` field provides the same continuation.

Private conversation reads, history search, and recent listings have been exercised
against the live service on macOS and Linux with Codex **0.154.0**. This verifies
those source operations for that version; it does not guarantee availability
for every account or future Codex/backend release. A global contextualize
installation is separate from testing a source checkout: verify discovery with
`contextualize plugins` after installing into your chosen environment.

claude.ai chat reads, listings, and search have been exercised against the live service
on Linux with Google Chrome 149. The macOS Keychain path is covered by tests
only.

## Development

```sh
uv sync --extra dev
uv run pytest
uv build
```

Tests use synthetic conversations and mocked transport; they need no account
or network. A live smoke test can use the `contextualize cat` commands above
after installation, with Codex signed in for ChatGPT or Chrome signed in to
claude.ai. It reads private history through that machine's signed-in account.

Nix users can build this plugin with `nix build`. When constructing a
contextualize environment with `mkContextualize`, include this source in
`extraPluginSrcs`; keep Codex on the runtime PATH.
