# Handoff: Hermes Agent x Open WebUI Memory UX

Date: 2026-04-24

This document is intended to be self-sufficient. The next engineer should not need any follow-up context from prior chat history.

## Goal

The downstream `galaxy-kuleon` customizations exist to make Hermes work well inside Open WebUI for multi-user deployments.

The product goal is:

- Hermes must remember each authenticated user's preferences, needs, and prior context accurately.
- Memory must be scoped per user by default, with optional shared memory only when explicitly configured.
- The UX must surface when Hermes recalled memory or found a resumable prior task, instead of acting like a black box.

In practice, this required bridging Open WebUI identity into Hermes API requests and exposing Hermes memory/continuation signals back to Open WebUI via SSE.

## Current Repo State

### Hermes Agent

- Repo path: `/root/hermes-agent`
- Current branch: `main-kg`
- Current HEAD: `1ee5731cf85e07ded5adfc9df22fad7c3449eba2`
- HEAD subject: `Merge pull request #3 from galaxy-kuleon/kuleon/main-kg-honcho-memory-squash`
- Working tree: clean
- Remotes:
  - `origin = https://github.com/galaxy-kuleon/hermes-agent.git`
  - `upstream = https://github.com/NousResearch/hermes-agent.git`

### Open WebUI

- Repo path: `/root/open-webui`
- Current branch: `main-kg`
- Current HEAD seen during inspection: `2490bd1a4`
- Recent relevant history includes:
  - `2490bd1a4 chore: remove .agent-team-waves from git tracking and add to .gitignore`
  - `a65d1c70f doc: update handoff`
  - `eb6ec9ca0 Merge pull request #3 from galaxy-kuleon/feat/v0.9.1-hermes-port`
- Working tree is **not clean**. Untracked items during inspection:
  - `ONBOARDING.md`
  - `backend/open_webui_data.zip`
  - `docker-compose.stack.override.yml`
  - `e2e/test-results/`
  - `screenshots/`
  - `test-results/`
- Remotes:
  - `origin = https://github.com/galaxy-kuleon/open-webui.git`
  - `upstream = https://github.com/open-webui/open-webui.git`

Do not blindly commit the Open WebUI untracked artifacts; they look like local setup/test outputs.

## What Was Already Merged Into Hermes `main-kg`

PR #3 merged the Hermes-side work needed for user-scoped memory and OWUI memory UX.

Important files on Hermes side:

- `gateway/platforms/api_server.py`
- `run_agent.py`
- `agent/_continuation_probe.py`
- `plugins/memory/honcho/session.py`
- Tests under `tests/gateway/`, `tests/honcho_plugin/`, `tests/e2e/`

The merged work includes:

1. Open WebUI identity headers are accepted by Hermes API server.
2. `user_id` and `tenant_id` are threaded into memory-provider initialization.
3. `hermes.memory.recalled` SSE is emitted on non-empty memory prefetch.
4. `hermes.continuation.suggested` SSE is emitted when a reasoning-capable provider finds an incomplete prior task.
5. `POST /v1/memory/tool` exists for authenticated memory admin/inspection flows.
6. Honcho compatibility shim translates `fact_store` admin calls onto Honcho tools.
7. Two-user isolation coverage exists.

## Integration Chain: End to End

The intended flow is:

1. Open WebUI authenticates a user.
2. Open WebUI resolves a stable Hermes identity:
   - `user_id`
   - `tenant_id`
3. Open WebUI sends these to Hermes via:
   - `X-Hermes-User-Id`
   - `X-Hermes-Tenant-Id`
4. Hermes initializes the configured memory provider with those identity values.
5. Hermes memory provider returns user-scoped recall / reasoning results.
6. Hermes API server emits SSE events for memory recall and continuation.
7. Open WebUI pipe translates those SSE events into frontend status actions.
8. Open WebUI UI renders the memory chip / continuation card.

If any step in that chain breaks, the product regresses back to generic stateless UX or, worse, cross-user leakage.

## Open WebUI: Identity and UX Logic Already Present

### 1. Open WebUI sends Hermes identity headers

File:

- `/root/open-webui/backend/open_webui/pipes/hermes_agent.py`

Key behavior:

- Calls `resolve_hermes_identity(__user__)`
- If resolved, adds:
  - `X-Hermes-User-Id`
  - `X-Hermes-Tenant-Id`

This is the critical bridge for per-user memory.

### 2. Open WebUI tenancy policy is intentionally isolation-first

File:

- `/root/open-webui/backend/open_webui/hermes/identity.py`

Rules:

1. Tier 1: `group.meta.tenancy` string wins.
2. Tier 2: `group.meta.shared_memory is True` uses `group.id` as tenant.
3. Tier 3: otherwise `tenant_id = user_id`.

This is deliberate. Groups do **not** implicitly share memory unless explicitly opted in.

That behavior is correct for the stated product goal: each user should keep accurate personal memory unless operators explicitly enable a shared tenant.

### 3. Open WebUI prevents IDOR on memory admin routes

File:

- `/root/open-webui/backend/open_webui/routers/hermes_memory.py`

Important behavior:

- Router ignores any spoofed identity in request body.
- Router always resolves identity from authenticated `current_user`.
- Router forwards only that resolved identity to Hermes `/v1/memory/tool`.

This is the necessary defense against one user reading or editing another user's memory.

### 4. Open WebUI already translates Hermes SSE into UX actions

File:

- `/root/open-webui/backend/open_webui/pipes/hermes_agent.py`

Implemented actions:

- `hermes.tool.progress` -> Open WebUI status event for tool progress
- `hermes.memory.recalled` -> `action = hermes_memory_recall`
- `hermes.continuation.suggested` -> `action = hermes_continuation`

### 5. Open WebUI UI already exists for both signals

Files:

- Continuation card:
  - `/root/open-webui/src/lib/components/chat/HermesContinuationCard.svelte`
  - `/root/open-webui/src/lib/components/chat/Chat.svelte`
- Memory recall chip:
  - `/root/open-webui/src/lib/components/chat/Messages/ResponseMessage/StatusHistory/HermesMemoryRecallStatus.svelte`
  - `/root/open-webui/src/lib/components/chat/Messages/ResponseMessage/StatusHistory/StatusItem.svelte`

So the UX layer is not speculative. It is already implemented and waiting for Hermes-side payload completeness.

## Hermes: What Exists Right Now

### 1. Identity headers are accepted on `/v1/chat/completions`

File:

- `/root/hermes-agent/gateway/platforms/api_server.py`

Current behavior:

- Reads `X-Hermes-User-Id` and `X-Hermes-Tenant-Id`
- Trims whitespace
- Treats empty-after-strip as absent
- Passes truthy values into `_run_agent(...)` and `_create_agent(...)`

### 2. `AIAgent` accepts and forwards identity to memory providers

File:

- `/root/hermes-agent/run_agent.py`

Current behavior:

- `AIAgent.__init__` accepts `user_id` and `tenant_id`
- During memory provider initialization, `_init_kwargs` forwards them into `MemoryManager.initialize_all(...)`

This is the core reason per-user memory is possible.

### 3. Hermes emits memory recall SSE

Files:

- `/root/hermes-agent/run_agent.py`
- `/root/hermes-agent/gateway/platforms/api_server.py`

Current behavior:

- After external memory prefetch, if non-empty, `memory_recall_callback` is invoked.
- API server turns that into:

```text
event: hermes.memory.recalled
data: {...}
```

### 4. Hermes emits continuation SSE

Files:

- `/root/hermes-agent/agent/_continuation_probe.py`
- `/root/hermes-agent/run_agent.py`
- `/root/hermes-agent/gateway/platforms/api_server.py`

Current behavior:

- Continuation probe runs only if a memory provider exposes a reasoning-style tool.
- If provider returns a non-empty, non-`[NONE]` summary, Hermes emits:

```text
event: hermes.continuation.suggested
data: {...}
```

### 5. Hermes exposes `/v1/memory/tool`

File:

- `/root/hermes-agent/gateway/platforms/api_server.py`

Current behavior:

- Authenticated endpoint
- Accepts `tool_name`, `args`, and optional `user_id` / `tenant_id`
- Builds a lightweight `MemoryManager`
- Loads configured provider
- Dispatches tool call under that scoped identity
- Includes Honcho `fact_store` compatibility path

This endpoint exists specifically so OWUI can manage memory as authenticated users rather than only through model turns.

## Known Gaps: What Is Still Missing or Incomplete

These are the important gaps as of this handoff.

### Gap 1: Hermes does **not** expose `POST /v1/continuation/probe`

Open WebUI already has a backend router for it:

- `/root/open-webui/backend/open_webui/routers/hermes_continuation.py`

But that OWUI router currently documents that Hermes does not ship the endpoint yet and falls back to:

```json
{"suggested": false}
```

Impact:

- SSE-driven continuation works during active chat turns.
- Proactive continuation on initial chat mount does **not** actually work end-to-end yet.

This is the single biggest Hermes-side functional gap remaining.

### Gap 2: Hermes recall SSE payload does not currently include `recalled_facts`

Open WebUI is already prepared for richer payloads:

- `/root/open-webui/backend/open_webui/pipes/hermes_agent.py`
- `/root/open-webui/backend/open_webui/test/pipes/test_hermes_memory_recall_facts.py`
- `/root/open-webui/src/lib/components/chat/Messages/ResponseMessage/StatusHistory/HermesMemoryRecallStatus.svelte`

OWUI expects optional `recalled_facts` entries shaped like:

```json
{
  "id": "42",
  "content_preview": "user likes blue",
  "score": 0.92
}
```

Hermes currently emits only:

- `provider`
- `context_preview`
- `context_token_estimate`

Impact:

- The memory chip appears.
- But the richer provenance list and per-fact forget UX are underutilized.

### Gap 3: Memory recall payload currently hardcodes provider label

In Hermes API server, memory recall events currently label provider as `holographic`.

Impact:

- If memory provider is switched to Honcho, the UI label becomes misleading.

This should be replaced with the actual active memory provider name from config/runtime.

### Gap 4: Integration completeness is strongest on `/v1/chat/completions`

The Open WebUI pipe is built around `/v1/chat/completions`.

That path is the one fully wired for:

- identity headers
- session continuity gate
- memory recall SSE
- continuation SSE

If future Open WebUI work shifts more Hermes usage to another API surface, verify identity and SSE parity there instead of assuming it exists automatically.

## What To Do Next (Recommended Order)

### Priority 1: Implement Hermes `POST /v1/continuation/probe`

Why first:

- Open WebUI already has the backend router and frontend client for it.
- It is the cleanest missing piece to complete the proactive continuation UX.

Suggested contract (already expected by OWUI):

Request:

```json
{
  "user_id": "alice",
  "tenant_id": "acme"
}
```

Response:

```json
{
  "suggested": true,
  "task_summary": "You were porting skip_rag.py to the new ABC.",
  "confidence": "low",
  "last_session_age_hours": 26
}
```

Recommended implementation strategy:

1. Add a new authenticated API server route in `gateway/platforms/api_server.py`.
2. Reuse `agent/_continuation_probe.py` logic instead of duplicating continuation logic.
3. Load the configured memory provider similarly to `/v1/memory/tool`.
4. Scope strictly by passed `user_id` / `tenant_id`.
5. Gracefully return `suggested: false` when provider lacks reasoning tools or returns `[NONE]`.

### Priority 2: Add `recalled_facts` to Hermes memory recall SSE payload

Why second:

- Open WebUI already has tests, UI, and forget interactions ready.
- This directly improves the user's trust in memory recall UX.

Recommended payload extension:

```json
{
  "provider": "honcho",
  "context_preview": "...",
  "context_token_estimate": 42,
  "recalled_facts": [
    {
      "id": "42",
      "content_preview": "user prefers dark mode",
      "score": 0.88
    }
  ]
}
```

Notes:

- OWUI already sanitizes/truncates `content_preview` to 200 chars.
- If exact fact provenance is unavailable for a provider, return `recalled_facts: []` rather than omitting the key.

### Priority 3: Replace hardcoded memory provider label

Make `provider` in memory recall SSE reflect actual configured provider, not a constant string.

### Priority 4: Tighten OWUI UX state handling

These are OWUI-side follow-ups, not Hermes blockers:

- Replace continuation-card dedup logic that currently relies on `document.querySelector(...)` in `Chat.svelte`.
- Make continuation dismiss state more durable so the same stale suggestion does not reappear too aggressively.
- Review `HermesMemoryRecallStatus.svelte` local state sync so optimistic fact removal is not accidentally reset by reactive updates.

## Exact Files To Touch For Each Next Step

### If implementing `/v1/continuation/probe` in Hermes

Primary:

- `/root/hermes-agent/gateway/platforms/api_server.py`
- `/root/hermes-agent/agent/_continuation_probe.py`

Likely tests to add/update:

- new test near `/root/hermes-agent/tests/gateway/test_continuation_probe.py`
- possibly add API-server route coverage in `tests/gateway/`

### If enriching recall payload with `recalled_facts`

Primary:

- `/root/hermes-agent/run_agent.py`
- `/root/hermes-agent/gateway/platforms/api_server.py`
- potentially provider-specific code under `plugins/memory/*`

OWUI side already ready:

- `/root/open-webui/backend/open_webui/pipes/hermes_agent.py`
- `/root/open-webui/src/lib/components/chat/Messages/ResponseMessage/StatusHistory/HermesMemoryRecallStatus.svelte`

## Validation Commands

### Hermes Agent

These targeted tests were already run successfully during this session before PR #3 merged:

```bash
cd /root/hermes-agent
scripts/run_tests.sh \
  tests/gateway/test_api_server_identity_header.py \
  tests/gateway/test_memory_recall_sse.py \
  tests/gateway/test_memory_tool_endpoint.py \
  tests/gateway/test_continuation_probe.py \
  tests/honcho_plugin/test_session.py \
  tests/e2e/test_honcho_two_user_isolation.py
```

Observed result then:

- `132 passed`
- `3 skipped`

If you add `/v1/continuation/probe`, also add and run a dedicated API-server route test.

### Open WebUI Backend

The repo has Python test dependencies in `pyproject.toml`, and the relevant test suites already exist.

Recommended targeted backend validation:

```bash
cd /root/open-webui
python -m pytest \
  backend/open_webui/test/hermes/test_identity.py \
  backend/open_webui/test/pipes/test_hermes_agent_headers.py \
  backend/open_webui/test/pipes/test_hermes_memory_recall.py \
  backend/open_webui/test/pipes/test_hermes_memory_recall_facts.py \
  backend/open_webui/test/pipes/test_hermes_continuation.py \
  backend/open_webui/test/routers/test_hermes_memory_router.py \
  backend/open_webui/test/routers/test_hermes_continuation_router.py
```

### Open WebUI Frontend / Typecheck

```bash
cd /root/open-webui
npm run check
```

### Open WebUI Frontend E2E (when live stack is available)

There is an existing Playwright spec for the memory chip:

- `/root/open-webui/e2e/tests/hermes-memory-chip.spec.ts`

Run it only against a real stack with Hermes reachable and memory provider configured.

## Operational Notes / Gotchas

1. `X-Hermes-Session-Id` is intentionally gated by auth.
   - OWUI only sends it when `hermes_api_key` is configured.
   - Hermes returns 403 for unauthenticated continuation attempts.

2. The product safety model depends on identity being server-derived.
   - OWUI routers must continue resolving identity from authenticated user state.
   - Do not accept client-supplied `user_id` / `tenant_id` as authoritative.

3. Group membership must not implicitly widen memory scope.
   - The explicit `shared_memory is True` rule in OWUI identity resolution is deliberate.

4. Open WebUI currently contains local untracked artifacts.
   - Review before commit.
   - Do not accidentally include screenshots/test outputs in functional PRs.

5. Hermes `main-kg` is already aligned to `origin/main-kg`.
   - This repo is clean now.
   - The merged Hermes work is in upstream branch history for `main-kg` on the galaxy-kuleon fork.

## Minimal Executive Summary

If you only remember five things, remember these:

1. The whole point of these customizations is accurate per-user memory in Open WebUI, not generic shared chat memory.
2. Open WebUI already sends `X-Hermes-User-Id` and `X-Hermes-Tenant-Id`; Hermes `main-kg` already consumes them and scopes memory providers with them.
3. Hermes already emits `hermes.memory.recalled` and `hermes.continuation.suggested` SSE on `/v1/chat/completions`.
4. Open WebUI already renders those signals in the UI.
5. The two most important missing pieces are:
   - Hermes `POST /v1/continuation/probe`
   - Hermes `recalled_facts` in memory recall payload

That is the highest-value next work.
