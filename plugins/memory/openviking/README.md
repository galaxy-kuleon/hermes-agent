# OpenViking Memory Provider

Context database by Volcengine (ByteDance) with filesystem-style knowledge hierarchy, tiered retrieval, and automatic memory extraction.

## Requirements

- OpenViking installed with the `openviking-server` command available
- OpenViking server config initialized and validated (`openviking-server init`,
  then `openviking-server doctor`)
- OpenViking server running and reachable from Hermes

OpenViking 0.2.10 or newer is recommended. For backward compatibility,
Hermes can identify older servers that expose the legacy status-only health
response, but only when anonymous OpenAPI metadata also identifies the service
as OpenViking. OpenViking 0.2.6 and earlier are deprecated for this integration;
upgrade them to receive the current health contract and compatibility fixes.

## Setup

Prepare OpenViking first:

```bash
openviking-server init
openviking-server doctor
openviking-server
```

Then configure Hermes:

```bash
hermes memory setup    # select "openviking"
```

The setup can link to an existing `~/.openviking/ovcli.conf`, copy its current
connection values into Hermes, or create a minimal `ovcli.conf` when one does
not exist.

Or manually:

```bash
hermes config set memory.provider openviking
```

Add the connection settings to the active profile's `.env` file. For the
default profile that is `~/.hermes/.env`; for a named profile use
`~/.hermes/profiles/<profile>/.env`.

```text
OPENVIKING_ENDPOINT=http://127.0.0.1:1933
# OPENVIKING_API_KEY=...
# OPENVIKING_ACCOUNT=default
# OPENVIKING_USER=default
```

## Config

OpenViking's server config is separate from Hermes:

- `ov.conf` configures OpenViking storage, embedding/VLM models, auth, and
  server behavior. OpenViking reads it from `--config`,
  `OPENVIKING_CONFIG_FILE`, or `~/.openviking/ov.conf`.
- `ovcli.conf` stores client/CLI connection values such as `url`, `api_key`,
  `account`, and `user`. It is read from `OPENVIKING_CLI_CONFIG_FILE` or
  `~/.openviking/ovcli.conf`.

Hermes-side provider config is read from environment variables in the active
profile's `.env`:

| Env Var | Default | Description |
|---------|---------|-------------|
| `OPENVIKING_ENDPOINT` | `http://127.0.0.1:1933` | Server URL |
| `OPENVIKING_API_KEY` | (none) | User/admin API key for authenticated servers |
| `OPENVIKING_ACCOUNT` | `default` | Tenant account for local/trusted mode |
| `OPENVIKING_USER` | `default` | Tenant user for local/trusted mode |
| `OPENVIKING_AGENT` | (none) | Optional peer ID for separate assistant context |

Ordinary requests send the configured account and request-bound gateway user
as identity headers, including when an API key is present. CLI requests use
the configured user fallback. OpenViking API-key mode derives identity from
the key and ignores these headers; trusted mode consumes the explicit
identity. Health probes remain anonymous first.
Hermes also sends `User-Agent: openviking-memory-hermes/<version>` on
OpenViking requests. This standard harness identifier contains the Hermes
version, but no per-user identifier, and does not add a separate request.

### Optional peer identity

New connections use the OpenViking user's memory directory by default. Setup
does not ask for a peer ID. Without a configured peer, Hermes sends neither
`X-OpenViking-Actor-Peer` nor assistant-message `peer_id`.

For separate assistant context, set the existing `agent` field in the active
profile's `config.yaml`:

```yaml
memory:
  openviking:
    agent: work-assistant
```

Existing non-empty `OPENVIKING_AGENT`, YAML `agent`, and linked OpenViking
`actor_peer_id` or legacy `agent_id` values retain their behavior. Resolution
order remains environment, linked OpenViking config, then Hermes YAML. To use
no peer, remove the peer value from each configured source and start a new
Hermes session.

Upgrades do not move or delete existing memories. Installations that relied
on the old implicit `hermes` peer now use user memory for new writes. Without
a peer ID, default OpenViking search covers user memory and existing peer
memories under the same OpenViking user. Old peer memories stay at their
existing paths and remain searchable. Ranking and result limits determine
which memories are returned. Keep a peer ID if you need the narrower view.

Set `agent: hermes` to restore peer-scoped writes. Memories written at user
scope before this change stay there and remain searchable. This setting
changes future writes, not the location of existing memories.

## Tools

| Tool | Description |
|------|-------------|
| `viking_search` | Semantic search with fast/deep/auto modes |
| `viking_read` | Read content at a viking:// URI (abstract/overview/full) |
| `viking_browse` | Filesystem-style navigation (list/tree/stat) |
| `viking_remember` | Store an explicitly classified fact with OpenViking `content/write` |
| `viking_forget` | Delete one exact `viking://` memory file URI |
| `viking_add_resource` | Ingest URLs/docs into the knowledge base |

## Memory Writes And Deletes

`viking_remember` writes directly through `POST /api/v1/content/write` with
`mode=create`. The category selects preferences, entities, events, cases,
or patterns; an omitted category defaults to cases. A preference requires
an exact evidence excerpt from a current user message. Attachment process
claims such as "fully read" are refused; coverage belongs in the attachment
coverage ledger. The response includes the canonical memory URI.

Memory URIs include the resolved user explicitly, for example
`viking://user/<user-id>/peers/hermes/memories/cases/mem_<id>.md`.
Without a configured peer, the path is directly under the user's memories.
Explicit remembers do not depend on session extraction.

Hermes' built-in `memory` store is not mirrored into OpenViking. Its `user`
target identifies the local USER.md profile but does not prove a preference,
and local entries have no stable OpenViking URI for replace/remove sync.
Automatic session commits extract only entities and events; automatic
profile/preferences, peer memory, and working memory extraction are disabled.

`viking_add_resource` requires the latest user message to explicitly request
adding, importing, or indexing the resource. Request-scoped attachment
handles are resolved with the current task's file grants before upload.

`viking_forget` is for explicit deletion of one concrete `.md` user memory
file. It reads the complete raw stored content and preserves the original,
source URI, SHA-256, and event timestamp in a unique append-only record at
`viking://user/<user-id>/signals/memory-deletions/<event-id>.json` before
deleting the active memory projection. These signals are not summarized,
vectorized, or returned by semantic retrieval. Historical deletion evidence
under `_observability` is also filtered from explicit and automatic recall.

Use explicit-user memory URIs. Legacy uid-less memory paths are expanded
using the active request identity before deletion. The tool rejects
directories, resources, skills, sessions, generated summaries, query strings,
and fragments. The `viking://~/...` alias is not accepted by this tool's
validator. Broader cleanup uses OpenViking's MCP, CLI, or admin APIs.
