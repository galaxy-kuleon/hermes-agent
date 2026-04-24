# Switching hermes-agent to self-hosted Honcho

Complete these steps once the honcho server is confirmed running at http://127.0.0.1:8000.

## Checklist

1. **Verify honcho server is up**
   ```
   curl http://127.0.0.1:8000/health
   ```
   Expect `{"status":"ok"}` or similar 200 response before proceeding.

2. **Create the hermes config directory (if absent)**
   ```
   mkdir -p ~/.hermes
   ```

3. **Place the honcho client config**
   ```
   cp /root/hermes-agent/honcho-self-host.json.draft ~/.hermes/honcho.json
   ```
   The client resolves `~/.hermes/honcho.json` as the default-profile config
   (`resolve_config_path()` in `plugins/memory/honcho/client.py:66`).

4. **Export the base URL env var** (makes the client active without an API key)
   ```
   export HONCHO_BASE_URL=http://127.0.0.1:8000
   ```
   For self-hosted instances the SDK accepts `"local"` as the API key placeholder
   (`client.py:654`), so no `HONCHO_API_KEY` is required.

5. **Do NOT export `HONCHO_API_KEY`** (leave it unset for a local instance)
   The client detects localhost/127.0.0.1 and substitutes `"local"` automatically.
   A cloud API key in scope here would be sent to the local server and rejected.

6. **Verify client config resolves correctly**
   ```
   python - <<'EOF'
   from plugins.memory.honcho.client import HonchoClientConfig
   cfg = HonchoClientConfig.from_global_config()
   print("enabled:", cfg.enabled)
   print("base_url:", cfg.base_url)
   print("workspace:", cfg.workspace_id)
   EOF
   ```
   Expected: `enabled: True`, `base_url: http://127.0.0.1:8000`.

7. **Set `memory.provider = honcho` in `~/.hermes/config.yaml`**
   ```
   python -c "
   import sys; sys.path.insert(0, '.')
   from hermes_cli.config import load_config, save_config
   cfg = load_config(); cfg.setdefault('memory', {})['provider'] = 'honcho'
   save_config(cfg); print('Done:', cfg['memory'])
   "
   ```
   Alternative one-liner if the `hermes` CLI is on PATH:
   ```
   hermes config set memory.provider honcho
   ```

8. **Confirm config.yaml was written**
   ```
   grep -A2 "^memory:" ~/.hermes/config.yaml
   ```
   Expected: `provider: honcho`.

9. **Smoke-test plugin load (no real session needed)**
   ```
   python - <<'EOF'
   from plugins.memory import load_memory_provider
   p = load_memory_provider("honcho")
   print("provider name:", p.name)
   print("is_available:", p.is_available())
   EOF
   ```
   Expected: `provider name: honcho`, `is_available: True`.

10. **Run the two-user isolation test** (requires honcho server running)
    ```
    HONCHO_BASE_URL=http://127.0.0.1:8000 pytest tests/e2e/test_honcho_two_user_isolation.py -v
    ```
    The test skips automatically when the server is unreachable; a PASSED result
    confirms per-user memory isolation is working end-to-end.

11. **Start hermes and verify no provider errors in the banner**
    ```
    hermes --quiet
    ```
    Look for `Honcho` in the startup output and absence of `"Honcho not configured"` warnings.

12. **Check the honcho status subcommand**
    ```
    hermes honcho status
    ```
    Should report the workspace, base URL, and recall mode without errors.

## Notes

- The auto-migration logic at `run_agent.py:1365-1392` will also activate honcho
  automatically the first time hermes starts if `memory.provider` is not set but
  a valid `honcho.json` with `enabled: true` and `baseUrl` is found. Step 7 above
  persists the setting explicitly so auto-migration only runs once.

- `honcho-self-host.json.draft` intentionally omits `apiKey`. Do not add a cloud
  API key to this file; the local instance does not validate it and a stale key
  causes misleading auth errors when the instance is later replaced with a cloud URL.
