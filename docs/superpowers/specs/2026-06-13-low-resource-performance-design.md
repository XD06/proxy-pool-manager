# Low Resource Performance Design

Date: 2026-06-13

## Context

Proxy Pool Manager is a local proxy control-plane service. Production proxy traffic is forwarded by sing-box, while the Python service handles node import, state persistence, config generation, engine lifecycle, port validation, GeoIP enrichment, ProxyAdmin integration, and local proxy quality checks.

The target deployment profile for this design is a low-cost server with 1 CPU core and 1 GB RAM. The expected node count is at most 300. The main goal is to keep resource usage predictable under batch operations, not to maximize benchmark throughput.

## Goals

- Keep the Python control plane lightweight enough for a 1 core / 1 GB RAM server.
- Avoid unbounded spikes from node tests, port validation, GeoIP lookup, ProxyAdmin calls, and proxycheck subprocesses.
- Reduce repeated full-state JSON writes during batch jobs.
- Keep the current sing-box forwarding model intact.
- Preserve the current API and UI behavior where practical.
- Add focused tests for resource-control behavior.

## Non-Goals

- Do not replace sing-box or redesign proxy forwarding.
- Do not introduce SQLite, Redis, Celery, or a separate worker service for this scale.
- Do not optimize for more than 300 active nodes in this phase.
- Do not remove existing synchronous endpoints, though large operations should prefer job endpoints.

## Resource Profile

Add a performance profile configuration in `config/app.json`, with environment-variable overrides where useful.

Recommended low-resource defaults:

```json
{
  "performance_profile": "low",
  "max_node_test_concurrency": 8,
  "max_port_test_concurrency": 8,
  "max_proxycheck_concurrency": 2,
  "max_geoip_concurrency": 2,
  "max_proxy_admin_concurrency": 8,
  "state_save_debounce_ms": 1000,
  "job_retention_minutes": 60
}
```

The normal profile should stay close to current behavior. The low profile should prefer stable peak usage over fastest completion.

## State Save Strategy

`StateStore.save()` currently writes the complete `AppState` JSON each time it is called. Batch jobs update state many times, so per-result saves amplify CPU and disk work.

Introduce a small save coordinator inside `create_app()` or near `StateStore`:

- `save_now()` writes immediately and updates the loaded mtime.
- `save_later()` schedules a debounced save after `state_save_debounce_ms`.
- `flush_save()` waits for any pending debounced save and writes immediately when needed.

Use immediate saves for small user actions such as import, assign, delete, subscription config, and engine start. Use debounced saves for streaming batch results from port validation, proxycheck, GeoIP enrichment, and other long jobs. Always flush on job completion, cancellation, or error.

This keeps UI progress in memory while avoiding hundreds of full JSON writes during a single batch.

## Unified Concurrency Limits

Centralize concurrency settings instead of hard-coding limits across modules.

Apply limits to these paths:

- Node testing through `test_nodes_with_temporary_engine()`.
- Port validation through `test_ports()` and `run_port_test_job()`.
- Local proxy quality checks through `run_local_proxy_check_job()`.
- GeoIP enrichment through `schedule_geoip_lookup()` and direct enrichment paths.
- ProxyAdmin import, delete, and quality check operations.

For low-resource mode:

- Node test concurrency: 8.
- Port validation concurrency: 8.
- Local proxycheck concurrency: 2, because each check starts or uses an external binary process.
- GeoIP concurrency: 2.
- ProxyAdmin concurrency: clamp user input to a low-resource maximum.

The job endpoints should remain responsive by streaming progress from completed tasks, but task creation should be bounded by semaphores or worker pools.

## Node Test Batching

Testing 300 nodes at once can create 300 temporary port mappings and a large temporary sing-box config. This is acceptable on stronger machines but risky on a 1 GB server.

In low-resource mode, split node tests into batches, for example 50 nodes per temporary engine run. Each batch should:

1. Generate a temporary sing-box config for that subset.
2. Start the temporary engine.
3. Validate nodes with bounded concurrency.
4. Stop the temporary engine.
5. Merge results into the job and state.
6. Debounce persistence.

Cancellation should stop scheduling new batches and then stop the current temporary engine in the existing cleanup path.

## Port Validation

Port validation currently does useful work, but it can multiply network requests quickly because each port can check multiple targets, then optionally query exit IP, then enrich GeoIP.

Optimize the path as follows:

- Add a bounded worker pool for assigned ports.
- Reuse existing target responses to extract exit IP before calling `query_exit_ip()`.
- Call `query_exit_ip()` only when no target produced an IP and at least one target succeeded.
- Prefer cached exit IP when fresh.
- Make GeoIP enrichment non-blocking for normal port validation responses in low-resource mode.

The main validation result should return promptly with alive status, delay, targets, and exit IP when known. GeoIP can be attached later from cache or background enrichment.

## GeoIP Strategy

GeoIP is useful metadata, but it should not dominate low-resource execution.

Rules:

- Always check `app_state.geoip_cache` first.
- Use a low concurrency semaphore.
- Avoid per-IP immediate saves; mark state dirty and rely on debounced persistence.
- Do not block batch validation on GeoIP unless a caller explicitly requests a blocking enrichment path.
- If lookup fails, cache the error with existing TTL semantics to avoid tight retry loops.

## HTTP Client Reuse

Repeatedly creating `httpx.AsyncClient` can waste connection setup and memory churn during batch operations.

Use shared clients inside batch scopes:

- ProxyAdmin import/check/remove should reuse one client per job or helper call where possible.
- Exit IP checks should reuse a client within the proxy and URL loop.
- Target validation already reuses one client per port; keep that pattern.

Shared clients should stay local to a job or request scope, not global, to avoid stale proxy configuration and shutdown complexity.

## Job Retention

In-memory job dictionaries currently retain completed jobs indefinitely. This can slowly grow memory usage on a long-running server.

Add cleanup for:

- Node test jobs.
- Port test jobs.
- ProxyAdmin jobs.
- Local proxycheck jobs.
- Canceled job marker sets if any marker survives unexpectedly.

Policy:

- Keep active jobs.
- Keep completed, canceled, and errored jobs for `job_retention_minutes`.
- Also enforce a maximum count per job type, such as 20 recent jobs.
- Run cleanup opportunistically when creating a new job and periodically from the existing app lifespan background context.

## sing-box Config and Engine Path

Keep the current one-port-per-inbound design. For 300 ports, the generated sing-box config is still reasonable and simpler than a dynamic routing redesign.

Low-risk optimizations:

- Only rewrite `config/sing-box.json` when mappings or runtime settings changed.
- Avoid duplicate `sing-box check` calls in the same start flow.
- Keep `sing-box check` before formal starts because it prevents bad configs from leaving the service in a confusing state.
- Use smaller temporary configs for low-resource node test batches.

## API and UI Behavior

Large operations should prefer asynchronous job endpoints:

- Node tests.
- Port tests.
- Local proxycheck checks.
- ProxyAdmin checks.

Synchronous endpoints can remain for compatibility and small operations. The UI should continue polling jobs as it does today, but backend persistence should be debounced rather than per-result.

## Error Handling

- If a debounced save fails, record the error and expose it through status or job error fields where practical.
- If a job is canceled, stop scheduling new work and flush current state after cleanup.
- If GeoIP fails, return the primary validation result without failing the whole port or node test.
- If proxycheck concurrency is saturated, queue work instead of starting extra subprocesses.
- If temporary engine startup fails for a batch, mark affected nodes with the engine error and continue only if safe to do so.

## Testing Plan

Add or update tests for:

- Low-resource settings are loaded and applied.
- Port validation uses bounded concurrency.
- Local proxycheck job clamps concurrency to the configured low-resource limit.
- Debounced saves collapse many updates into fewer writes and flush at job completion.
- Job cleanup removes expired jobs and keeps active jobs.
- GeoIP background failure does not fail the main validation result.
- Node test batching produces results equivalent to a single batch for representative inputs.
- Existing API smoke tests remain compatible.

## Implementation Order

1. Add performance settings helpers and defaults.
2. Add save coordinator and convert batch jobs to debounced saves.
3. Add job cleanup.
4. Add port validation and proxycheck concurrency controls.
5. Add low-resource node test batching.
6. Move GeoIP enrichment to cache-first, non-blocking behavior where appropriate.
7. Reuse HTTP clients in ProxyAdmin and exit IP batch paths.
8. Run compile and test suite.

## Acceptance Criteria

- With low-resource mode enabled, batch jobs do not create unbounded tasks for 300 nodes or ports.
- A 300-node test completes in bounded batches without leaving a temporary sing-box process behind.
- Batch jobs produce progress while writing state at a controlled rate.
- Completed job metadata is automatically cleaned up.
- Existing documented workflows continue to work.
- The test suite passes.
