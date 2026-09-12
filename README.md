# cx-cgpt

A [contextualize](https://github.com/jmpaz/contextualize) source plugin for
ChatGPT conversations, history search, and recent-history listings. Runs on
macOS and Linux using the local Codex login.

## Install

Install the plugin into the **same Python environment as contextualize**. For a
fresh isolated installation from this checkout:

```sh
uv venv .venv
uv pip install --python .venv/bin/python /path/to/contextualize /path/to/cx-cgpt
.venv/bin/contextualize plugins
```

The plugin should appear as the `chatgpt` source. Activate that environment
before using the commands below, or invoke `.venv/bin/contextualize` directly.
For a uv-managed command, use
`uv tool install --with /path/to/cx-cgpt /path/to/contextualize`. When extending an
existing installation, preserve its other plugin dependencies. Installing
cx-cgpt into an unrelated environment will not make it discoverable.
The plugin has no dependency on a particular contextualize checkout.
Codex must be on PATH and signed in with ChatGPT (`codex login`). Set
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
Public `/share/` URLs and custom GPT `/g/` or gizmo URLs are not supported;
use a supported `/c/<conversation-id>` address for an accessible conversation.

`chatgpt:UUID` is also accepted. Transcripts retain the title, conversation and
message identities, timestamps, speaker roles, model names, tool messages, and
structured content. They follow `current_node` back through its ancestors:
this is the selected branch, not an interleaving of alternative responses.
Broken or missing ancestry is explicitly marked incomplete. Nothing is clipped
by the plugin; contextualize's own output limits still apply.

`?output=json` returns the conversation object, including alternative branches
present in its mapping. Attachment references and structured media parts are
preserved, but attachment and media bytes are not downloaded. Historical
message text is source material, not instructions to the consuming agent.

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
read their conversations. The default result limit is 20, maximum 100.

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

Running a live read authorizes the plugin to use the local Codex ChatGPT
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
- **Unsupported response schema:** the backend interface may have changed.
  Report the error and Codex/plugin versions without including private
  conversation bodies or authentication data.

## Compatibility and verification

Use a contextualize version that preserves JSON listing envelopes. If
`cat --list --json` prints Markdown, update contextualize or read the plugin's
structured page with `contextualize cat 'chatgpt:threads?output=json'` instead;
its `next_target` field provides the same continuation.

Conversation reads, history search, and recent listings have been exercised
against the live service on macOS and Linux with Codex **0.154.0**. This verifies
those source operations for that version; it does not guarantee availability
for every account or future Codex/backend release. A global contextualize
installation is separate from testing a source checkout: verify discovery with
`contextualize plugins` after installing into your chosen environment.

## Development

```sh
uv sync --extra dev
uv run pytest
uv build
```

Tests use synthetic conversations and mocked transport; they need no account
or network. A live smoke test can use the `contextualize cat` commands above
after installation and Codex login. It reads private history through that
machine's signed-in account.

Nix users can build this plugin with `nix build`. When constructing a
contextualize environment with `mkContextualize`, include this source in
`extraPluginSrcs`; keep Codex on the runtime PATH.
