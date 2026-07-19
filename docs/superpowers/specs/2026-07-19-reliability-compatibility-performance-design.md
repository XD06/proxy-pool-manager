# Reliability, Compatibility, and Performance Design

Date: 2026-07-19

## Objective

Make Proxy Pool Manager reliable with large, mixed subscriptions while preserving the current node-pool and traffic features. A node must expose an explainable health state, the web UI must not block on runtime inspection, and a node pool must continue to expose one public port regardless of member count.

## Confirmed constraints

- Preserve all existing nodes, mappings, groups, pools, and traffic history.
- Keep sing-box as the protocol engine and the Go pool-router as the public L4 pool edge.
- Do not equate reachability or TCP latency with usable proxy status.
- Keep node-pool policies: round robin, weighted round robin, and time window.
- Make the monitoring overview the primary page and keep notifications outside document flow.

## Architecture decisions

### Runtime state and traffic

`PoolRouterManager.payload()` must no longer execute process discovery in an HTTP request. A background runtime-state refresher owns process discovery and control-endpoint health checks. HTTP endpoints read a last-known snapshot with freshness metadata.

One traffic sampler owns calls to pool-router `/status` and persists counters. Traffic overview and entity endpoints only read the store. If router control is temporarily unavailable, endpoints return the last snapshot with `telemetry_stale=true` rather than failing with 500.

### Parser and diagnostics

Parsing is split into normalization, protocol mapping, local sing-box validation, and structured diagnostics. Every input field is classified as mapped, intentionally ignored, or unsupported. Unsupported transports never silently pass through into runtime configuration.

Each node receives a diagnostic classification such as `parse_error`, `config_incompatible`, `transport_unsupported`, `reachable`, `proxy_verified`, or `target_blocked`.

### Testing modes

Testing is separated into three modes:

1. Configuration preflight: parse and `sing-box check`; never claims network availability.
2. Reachability scan: low-cost network reachability; never auto-enables a node for a pool.
3. Full proxy verification: complete protocol handshake and one Google 204 request; this is the condition for automatic pool eligibility.

Exit-IP and GeoIP lookup are separate, opt-in work and are not run for every node by default. Test execution uses bounded, adaptive concurrency. Test-only loopback listeners are private and are distinct from public pool ports.

### Pool behavior

A pool exposes exactly one public listener. Pool members have explicit enablement, verification, failure, and draining state. Routing decisions affect new connections only; existing connections drain normally. The router records selection and per-member counters.

### Frontend data flow

The browser fetches a page-specific node read model, never both a full node list and the same paged list during initial load. Test job responses provide a cursor or delta since the previous poll. Only visible rows and compact summaries update during a job.

The assignment view uses pagination or virtualization. Monitoring uses an information-dense KPI strip, compact empty states, a smooth traffic chart when data exists, and a bounded active-record list. Toasts remain fixed and aria-live without changing page layout.

## Delivery phases

### Phase 0 — Baseline and safety

- Add regression fixtures for AnyTLS, VLESS Reality/WS, Hysteria2, and TUIC.
- Capture API latency, page-load trace, and 300-node render/test benchmarks.
- Add data-preservation tests and backup assertions for state migration paths.

### Phase 1 — Runtime and traffic stability

- Move Router process discovery off request paths.
- Add cached runtime snapshot and a single-flight control refresh.
- Make traffic sampling single-owner and stale-aware.
- Verify no telemetry request can return 500 solely because the control endpoint times out.

### Phase 2 — Parsing and compatibility diagnostics

- Implement explicit transport mapping and unsupported-field diagnostics.
- Add VLESS packet encoding, HTTPUpgrade and expanded Clash transport support.
- Add missing safe TUIC/Hysteria2 mappings.
- Preflight generated configurations before scheduling network tests.

### Phase 3 — Layered testing

- Implement test mode contracts and result classifications.
- Default full verification target to Google 204.
- Make exit-IP lookup opt-in and remove it from all-node default testing.
- Add bounded concurrency, cancel behavior, and result deltas.

### Phase 4 — Pool health behavior

- Feed verified health into member eligibility.
- Implement automatic isolation, draining, and recovery state transitions.
- Add deterministic tests for all three selection policies and long-connection behavior.

### Phase 5 — Frontend performance and visual hierarchy

- Split the monolithic app script by feature boundary.
- Replace full assignment re-renders with paged/virtual rows and incremental updates.
- Remove duplicate node fetches and use view-specific API payloads.
- Redesign monitoring density, empty states, responsive tables, and non-layout-shifting toasts.

## Acceptance criteria

- No node, mapping, group, pool, or traffic data is deleted by migration.
- Local homepage P95 is below 200ms and `/api/status` P95 below 300ms when runtime is healthy.
- Router control timeouts produce stale telemetry metadata, never an HTTP 500 monitoring page.
- Import diagnostics identify invalid, unsupported, and untested nodes separately.
- Only full proxy verification marks a node eligible for automatic pool membership.
- A 300-node active test does not rebuild the full assignment table once per poll.
- Full test suite, parser fixtures, Go router tests, and browser performance trace pass before release.

## Out of scope

- Replacing sing-box with a custom protocol engine.
- Treating a bare TCP connection as proof of proxy usability.
- Rewriting the Go router into a protocol-aware outbound implementation.
