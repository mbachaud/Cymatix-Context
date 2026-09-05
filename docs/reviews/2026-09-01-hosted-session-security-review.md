# Hosted Session Security, RBAC, and Encryption Review

**Date:** 2026-09-01  
**Status:** Architecture and source review  
**Target environment:** One Cymatix Context hosted session running in a
Proxmox/Ceph cluster and accessed by users and agents from other cluster VMs

## Executive summary

Cymatix Context is not currently safe to expose directly to peer VMs as a
multi-user hosted service. Its present security model is local and trusted:
most reads and session operations are unauthenticated, client-supplied identity
and session identifiers are trusted, authorization is not consistently applied
to every retrieval path, the server defaults to plaintext HTTP, and the
datastore and its derived artifacts are stored in plaintext.

The recommended deployment model is one isolated Cymatix data-plane deployment
per hosted session. Each deployment receives its own VM or service instance,
SQLite datastore, Ceph RBD image, encryption key, workload credentials,
firewall policy, backup namespace, and audit stream. A shared control plane may
provision and manage deployments, but users and agents reach a deployment only
through an authenticated HTTPS gateway.

The recommended domain split is:

```text
www.cymatixcontext.com
  Marketing and documentation

app.cymatixcontext.com
  Login, account management, and deployment control plane

d-<opaque-id>.cymatixcontext.app
  One isolated hosted-session data plane: UI, API, and MCP
```

Owning `cymatixcontext.app` creates a useful browser and operational boundary
from the `.com` control plane and provides HSTS-preloaded HTTPS. It does not,
by itself, isolate deployments. Isolation must also be enforced in identity,
authorization, compute, networking, storage, encryption, backup, and telemetry.

## Scope and limitations

This was a read-only review of the application source, configuration,
deployment templates, tests, and relevant primary security documentation. It
was not a live audit of the Proxmox cluster, Ceph configuration, firewall,
identity provider, DNS, certificates, or backup system. Those controls must be
verified against the running environment before production exposure.

## Existing strengths

- The server binds to loopback by default.
- A warning is emitted when it is bound beyond loopback without an admin token.
- Admin-token comparison uses a constant-time operation.
- Selected administrative, ingest, and consolidation endpoints are covered by
  the optional admin token.
- The documentation explicitly identifies the current TOFU/local-trust model.
- Vault writes include containment and atomic-write protections.
- Existing tests enumerate important portions of the administrative route
  surface.

These controls are useful defense in depth, but they are not a hosted-service
authentication or authorization system.

## Findings

### Critical when deployed: exposed observability defaults

The bundled observability Compose configuration publishes OTLP, Prometheus,
Tempo, Loki, and Grafana ports on all host interfaces. Grafana is configured
with `admin/admin` credentials and anonymous Viewer access, while Loki has
authentication disabled. If this template is used on a VM reachable by other
cluster guests, it creates a direct telemetry and operational-data exposure.

Evidence:

- `deploy/otel/docker-compose.yml` (ports at lines 8-11, 29-30, 40-41,
  51-52, 68-69 published without a host bind; Grafana
  `GF_SECURITY_ADMIN_USER/PASSWORD=admin` and
  `GF_AUTH_ANONYMOUS_ENABLED=true` at lines 59-62)
- `deploy/otel/loki-config.yaml` (`auth_enabled: false`, line 1)
- `deploy/otel/otel-collector-config.yaml`

Tracked as [#434](https://github.com/mbachaud/Cymatix-Context/issues/434).

### High: the application is not authenticated by default

The configured admin token defaults to empty. When empty, its authentication
dependency returns without checking a credential. Even when configured, it
protects only selected routes. Context reads, session registration, announce,
heartbeat, manifests, HITL operations, direct gene access, debug paths, replica
sync, bridge collection, and some vault operations remain outside a complete
deny-by-default policy.

The existing test suite deliberately verifies that non-administrative routes
remain accessible when the admin token is enabled. This is correct for the
current local-trust product contract, but not for a hosted service.

Evidence:

- `cymatix_context/config.py`, server configuration
- `cymatix_context/server/helpers.py`, `build_admin_auth`
- `cymatix_context/server/routes_registry.py`
- `cymatix_context/server/routes_admin.py`
- `tests/test_admin_auth.py`

### High: caller-controlled identity and session identifiers

Context and ingest requests accept identifiers such as `session_id`,
`party_id`, `participant_id`, `agent_id`, and organization attribution from the
request. Registration follows a trust-on-first-use model, and subsequent
announce, heartbeat, and manifest operations do not establish cryptographic
ownership of a participant or session.

These values are useful provenance metadata, but they cannot be authorization
inputs. In particular, `party_id` represents a party or device and should not
become the tenant boundary.

Evidence:

- `cymatix_context/server/routes_context.py`
- `cymatix_context/server/routes_ingest.py`
- `cymatix_context/server/routes_registry.py`
- `docs/architecture/SESSION_REGISTRY.md`
- `docs/architecture/FEDERATION_LOCAL.md`

### High: authorization is inconsistent across retrieval paths

The optional lexical party filter applies only to part of retrieval and
deliberately leaves unattributed documents visible. Equivalent authorization
is not consistently applied at dense candidate generation, packet retrieval,
direct identifier reads, debug paths, graph traversal, and other derived-index
lookups.

Hosted authorization must filter before or during candidate generation in
every retrieval tier. Retrieving cross-tenant candidates and filtering them
afterward is not an adequate boundary because content, scores, existence, and
timing can leak.

Evidence:

- `cymatix_context/knowledge_store.py`, `query_docs` and dense recall
- `cymatix_context/context_packet.py`
- `cymatix_context/server/routes_context.py`
- `cymatix_context/server/routes_admin.py`

### High: transport and client authentication are absent

The application starts Uvicorn without TLS and the MCP HTTP client defaults to
`http://127.0.0.1:11437`. The MCP client forwards identity-shaped metadata but
does not send an authorization credential or workload certificate.

Remote users require OIDC-based authentication. Remote agents require their
own workload identity, preferably short-lived mTLS credentials, and a
deployment-scoped access token or explicit delegation when acting for a user.

Evidence:

- `cymatix_context/server/app.py`
- `cymatix_context/mcp/mcp_server.py`

### High: datastore and derived artifacts are plaintext

The primary store uses standard SQLite in WAL mode without SQLCipher or
application-layer encryption. The vault can write original content to disk,
its state is another SQLite database, and redaction is disabled by default.
Replicas and backup files are likewise plaintext unless the surrounding volume
or backup system encrypts them.

The complete security boundary includes:

- SQLite database, WAL, and SHM files
- lexical and dense indexes and embeddings
- vault content and vault state
- replicas, snapshots, exports, and backups
- telemetry and diagnostic output

Embeddings and indexes are derived data but can still reveal sensitive
information and must receive the same tenant and encryption treatment.

Evidence:

- `cymatix_context/knowledge_store.py`
- `cymatix_context/vault/writer.py`
- `cymatix_context/vault/state.py`
- `cymatix_context/config.py`, vault defaults
- `cymatix_context/persistence.py`

### Medium: secret, audit, abuse, and recovery gaps

- The admin token is a static secret read from TOML with no expiry, scopes,
  revocation, rotation protocol, or environment/secret-manager integration.
- Project documentation directs the operator to place the secret in
  `cymatix.toml` (`docs/config-reference.md`, `[server]` example,
  `# admin_token = "your-secret"`), and `cymatix.toml` is tracked by the
  repository and not excluded by `.gitignore`. The tracked file contains no
  `admin_token` key or secret value today; the exposure is the pattern, not a
  committed credential.
- There is no complete allow/deny authorization audit trail.
- Request-size and rate limits are absent.
- Query telemetry retains a plaintext prefix rather than fully redacting it
  (`cymatix_context/telemetry/otel.py`, `_redact_query`: first 50 characters
  plus a SHA-256 tag when `redact_query = true`).
- A plain file copy of `genome.db` is unsafe for a live WAL-mode database.
  The current operator runbook (`docs/operator-runbooks.md`, backfill
  pre-flight and WAL-recovery sections) does stop the service first; the
  archived README plan (`docs/archive/plans/2026-05-08-readme-v2.md`) shows
  an unconditional `cp`. A consistent SQLite backup API, coordinated
  snapshot, or stopped service is required, and that requirement is not yet
  part of the product contract.
- Backup integrity, retention, key recovery, and restore drills are not part of
  the current product contract.

## Target security architecture

### Trust planes

```text
User VM  -- OIDC access/session credential --\
                                           +--> HTTPS gateway :443
Agent VM -- mTLS identity + scoped token ---/           |
                                                       | private mTLS
                                               Active Cymatix VM
                                                       |
                                                guest LUKS volume
                                                       |
                                              one Ceph RBD image
                                                       |
                                             dm-crypt Ceph OSDs
```

The architecture has three distinct planes:

1. **Application data plane:** users and agents reach only the HTTPS gateway.
2. **Storage plane:** the active Cymatix VM sees a normal guest block device
   backed by Ceph RBD. Client VMs never receive Ceph keyrings, RBD mappings, or
   database access.
3. **Management plane:** Proxmox, corosync, Ceph administration, PKI/KMS,
   backup, and telemetry are isolated from guest application networks.

### Single-writer requirement

Run one active Cymatix writer for a datastore. Ceph makes an RBD image
available to Proxmox nodes for VM mobility; it does not make concurrent SQLite
writes from multiple VMs safe.

Proxmox HA may restart or migrate the active VM, but promotion requires fencing
and an application lease or epoch check so the former owner cannot continue
writing. Do not multi-mount SQLite over CephFS or attach one RBD image
read/write to multiple application VMs. If active-active application instances
become a requirement, persistence should be redesigned around a transactional
HA database such as PostgreSQL.

## RBAC model

### Resource hierarchy

```text
tenant -> corpus/datastore -> hosted session -> documents and events
```

For the recommended one-deployment-per-hosted-session model, VM and database
separation provide the outer tenant boundary. RBAC still controls which users
and agents may act inside that deployment.

Minimum persistent authorization records:

- `principals`: user and workload identities, including IdP subject or SPIFFE ID
- `roles` and `permissions`
- `role_bindings`: principal, role, scope type, and scope ID
- `sessions`: tenant, corpus, owner, epoch, and status
- `session_memberships`: principal, role, expiry, and status
- `delegation_grants`: delegator, agent/workload, scopes, audience, and expiry
- `audit_events`: actor, delegator, action, resource, allow/deny, request ID,
  source, and timestamp

Every stored and derived object must carry an immutable deployment/tenant and
corpus scope. Session IDs must be server-issued, high-entropy identifiers and
must never function as bearer credentials.

### Suggested roles

| Role | Intended authority |
|---|---|
| `session.owner` | Session lifecycle, membership, read, and write |
| `session.editor` | Query, ingest, and update |
| `session.reader` | Query and read only |
| `session.agent` | Only explicitly delegated query/write scopes |
| `corpus.steward` | Retention, indexing, consolidation, and compaction |
| `app.operator` | Health, deployment, and configuration; no content read by default |
| `auditor` | Authorization and operational audit metadata |
| `platform.storage-admin` | Proxmox/Ceph administration only; no application RBAC |

Application and infrastructure roles must remain separate. A Proxmox or Ceph
administrator is not automatically a Cymatix session owner, and an application
owner receives no Ceph credentials.

### Authorization decision

For a user request:

```text
allow = authenticated
    AND token signature, issuer, audience, and lifetime are valid
    AND request host resolves to a provisioned deployment
    AND token deployment/tenant matches that host
    AND active session membership exists
    AND the scoped role permits the action
    AND every datastore operation is constrained to that scope
```

For an agent acting under delegation:

```text
effective permissions =
    workload identity ceiling
  INTERSECT delegated user scopes
  INTERSECT session role permissions
```

Audit both the workload actor and human delegator. An autonomous service agent
without human delegation should instead have its own workload principal and an
explicit session membership.

## Domain and browser isolation

### Recommended split

Use `app.cymatixcontext.com` as the control plane and
`d-<opaque-id>.cymatixcontext.app` as the deployment data plane. This is
preferable to placing deployments at
`d-<id>.app.cymatixcontext.com` because the `.app` deployment plane is a
different registrable browser site from the `.com` control plane.

Each deployment subdomain is a different browser origin because its hostname
differs. However, all `*.cymatixcontext.app` deployments remain same-site with
one another for browser features based on the registrable domain. Cross-tenant
security must therefore come from authenticated deployment context and
authorization, not DNS naming.

The `.app` top-level domain is HSTS-preloaded, so browsers require HTTPS for
all `.app` hostnames. This prevents accidental HTTP operation but also means
that every environment, including staging, needs a valid certificate from its
first browser connection.

### Cookie rules

Each deployment sets its own host-only session cookie:

```text
Set-Cookie: __Host-session=<opaque>;
            Secure; HttpOnly; Path=/; SameSite=Lax
```

Do not set a `Domain` attribute and never issue cookies for
`.cymatixcontext.app` or `.cymatixcontext.com`. A domain cookie would widen the
credential scope to sibling subdomains. Use `SameSite=Strict` where the login
and navigation flow permits it.

### OIDC flow

Prefer a central, exact callback such as:

```text
https://app.cymatixcontext.com/oauth/callback
```

After successful OIDC Authorization Code + PKCE authentication, use a
short-lived, single-use launch code bound to the deployment, intended redirect,
user, nonce, and expiry. The destination deployment exchanges it server-side
and creates its own host-only session. Do not move access or refresh tokens
between domains in URL parameters.

Do not register wildcard OAuth redirects or accept a caller-controlled
`return_to` host. OAuth redirect URIs must use exact matching, and the final
deployment host must come from the provisioned deployment registry.

Agents do not use browser cookies. They authenticate with an individual
short-lived workload certificate and a token whose audience and scopes are
specific to the destination deployment.

### DNS, TLS, and routing

- Maintain an authoritative `hostname -> deployment_id -> private backend`
  registry.
- Canonicalize and allowlist `Host`/SNI at the gateway; reject unknown hosts.
- Bind the authenticated deployment claim to the resolved host on every
  request. Never trust deployment or tenant IDs supplied in a body, query, or
  arbitrary peer header.
- Prefer same-origin UI, API, and MCP paths under the deployment host.
- If CORS is necessary, allow only explicitly provisioned origins. Never
  reflect arbitrary origins or combine credentialed requests with a wildcard.
- Provision and remove DNS and certificates atomically to prevent dangling
  records and subdomain takeover.
- A wildcard certificate is acceptable only when its private key remains in a
  hardened central gateway. Do not distribute that key to every VM. Managed
  per-deployment certificates provide a smaller key-compromise blast radius.

For deployments that execute tenant-controlled active web content, plugins, or
arbitrary HTML, sibling `.app` subdomains are not the strongest available
browser boundary. Use separate registrable/custom domains or a deliberately
sandboxed content origin for that higher-risk content.

## Proxmox and Ceph controls

### Network segmentation

Separate, by VLAN/VRF and firewall policy where practical:

- Proxmox management
- corosync
- Ceph public/client traffic
- Ceph cluster replication and heartbeat traffic
- public or cluster-local application ingress
- private gateway-to-application service traffic
- observability
- backup, KMS, and identity-provider traffic

Allow peer user/agent VMs to reach only gateway TCP 443. Deny them access to
Proxmox management, corosync, Ceph networks, datastore ports, telemetry
services, and KMS administration. Use physically separate interfaces for
latency- and trust-sensitive networks where available.

### Ceph authentication

CephX authenticates Ceph services and storage clients; it is not application
user or agent RBAC. Use a least-privileged Proxmox Ceph principal restricted by
pool and source network. Do not place `client.admin` or any Ceph keyring inside
client/agent VMs.

Explicitly require and verify Ceph msgr2 `secure` mode for cluster and client
connections. Compatible defaults may permit both CRC and secure modes; CRC
does not provide confidentiality against a malicious network observer.

## Encryption design

Apply encryption in layers:

1. **Client transport:** TLS 1.3 at the deployment gateway.
2. **Workload transport:** mTLS for agents and gateway-to-application traffic.
3. **Ceph transport:** msgr2 secure mode.
4. **Physical media:** Ceph OSD dm-crypt/LUKS.
5. **Deployment data:** guest-level LUKS on the Cymatix data volume. This
   protects RBD snapshots when the unlock key is held outside the Ceph trust
   plane.
6. **Backups:** authenticated client-side encryption, with backup recovery keys
   held separately from the cluster and routinely tested.
7. **Optional application envelope encryption:** per-deployment or per-corpus
   data-encryption keys wrapped by an external KMS/HSM/Vault key.

Whole-volume encryption is the sensible first milestone because it is
transparent to SQLite and its search indexes. SQLCipher could add database-page
encryption, but it does not cover vault files, exported replicas, telemetry, or
all backup paths. Application envelope encryption provides stronger separation
from storage administrators but complicates searchable indexes and still
allows the running application to decrypt active content.

Never store the only usable encryption key in the same TOML file, VM disk,
Ceph pool, or backup set as the protected data. Define bootstrap, rotation,
revocation, recovery, and offboarding procedures before production use.

## Implementation priorities

### P0: required before non-loopback hosted exposure

1. Put Cymatix behind an HTTPS gateway; block direct peer access.
2. Add authenticated request context and a deny-by-default route-policy map.
3. Replace caller-controlled authorization identities with server-derived
   principal, deployment, tenant, and session context.
4. Enforce scope during candidate generation and lookup in every lexical,
   dense, packet, graph, direct-ID, cache, replica, and vault path.
5. Protect or disable the default observability deployment.
6. Run exactly one fenced SQLite writer per datastore.
7. Keep all Ceph credentials and networks inaccessible to client VMs.

### P1: production security baseline

1. Add OIDC user identity and mTLS workload identity.
2. Add scoped roles, bindings, session memberships, delegation grants, and
   authorization audit events.
3. Issue short-lived, deployment-audienced tokens and rotate all secrets.
4. Enable CephX least privilege, msgr2 secure mode, OSD encryption, guest data
   encryption, and encrypted backups.
5. Add request-size limits, rate limits, trusted-host enforcement, strict CORS,
   secret scanning, and fail-closed production configuration.
6. Run backup restore, key recovery, failover, fencing, and offboarding drills.

### P2: higher assurance and scale

1. Add per-deployment or per-corpus application envelope encryption where the
   threat model requires separation from storage/platform administrators.
2. Adopt policy-as-code, authorization regression tests, and configuration
   drift detection.
3. Move to a transactional HA database before adding active-active application
   writers.
4. Perform a live cluster configuration audit and an external penetration test.

## Validation performed

The focused authentication, session registry, and delivery tests completed
successfully:

```text
142 passed
```

(That count is from the original review checkout, `ddfdf96`. On `beta`
`85b2c82` the equivalent set — `tests/test_admin_auth.py`,
`test_registry.py`, `test_session_delivery.py`, `test_bridge_registry.py`,
`test_packet_delivery_visibility.py` — is `180 passed`.)

These tests validate the current local-trust behavior; they do not demonstrate
hosted multi-tenant isolation. A future security suite should enumerate every
route and retrieval tier, test cross-deployment and cross-session denial, test
agent delegation intersections, and verify both allow and deny audit events.

## Verification against beta

Verified against beta `85b2c82` on 2026-09-02. The review was written
against a stale checkout (`ddfdf96`, 169 commits behind `beta`), so every
source pointer above was re-resolved against `beta` before this document
was committed.

All 20 file pointers resolve on `beta`, and the following claims were
re-confirmed line-by-line: loopback default bind
(`cymatix_context/config.py:266`), empty `admin_token` default
(`config.py:279`) with the early return in `build_admin_auth`
(`cymatix_context/server/helpers.py:78-102`, `hmac.compare_digest` at
line 97), the non-loopback startup warning
(`cymatix_context/server/app.py:96-104`), `uvicorn.run` without TLS
(`app.py:244`), the MCP client default `http://127.0.0.1:11437` and
`Content-Type`-only headers (`cymatix_context/mcp/mcp_server.py:130,246`),
the unauthenticated registry/HITL/manifest/expand routes
(`routes_registry.py`), the unauthenticated `/genes/{id}`, `/debug/*`,
`/replicas/sync`, `/bridge/collect`, `/bridge/signal` routes
(`routes_admin.py`) and `/vault/*` routes (`server/helpers.py:1179-1208`),
the Tiers 1-3-only party filter that keeps unattributed documents visible
(`cymatix_context/knowledge_store.py:2965-2990`), the vault
`redact_body = False` default (`config.py:1365`), and the Compose/Loki
evidence for the critical finding (line numbers now inline above).

Pointers that drifted and were corrected in place (findings kept):

- **Secret in `cymatix.toml`.** The tracked `cymatix.toml` on `beta` has no
  `admin_token` key and no secret values; the docs example is a commented
  placeholder. The bullet now states that the pattern, not a committed
  credential, is the exposure.
- **`cp genome.db` backup.** The live `docs/operator-runbooks.md` stops
  the service before copying; only the archived
  `docs/archive/plans/2026-05-08-readme-v2.md` shows an unconditional `cp`.
  The bullet now cites both.
- **Focused test count.** `142 passed` was the stale checkout; the `beta`
  count is recorded next to it.

## Primary references

- [Google Registry: `.app` HSTS preload](https://get.app/)
- [MDN: Same-origin policy](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Same-origin_policy)
- [MDN: Set-Cookie and `__Host-`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)
- [OAuth 2.0 Security Best Current Practice, RFC 9700](https://datatracker.ietf.org/doc/html/rfc9700)
- [OAuth 2.0 Mutual TLS, RFC 8705](https://datatracker.ietf.org/doc/html/rfc8705)
- [SPIFFE concepts](https://spiffe.io/docs/latest/spiffe/concepts/)
- [OWASP Multi-Tenant Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Multi_Tenant_Security_Cheat_Sheet.html)
- [Ceph msgr2 security modes](https://docs.ceph.com/en/umbrella/rados/configuration/msgr2/)
- [Ceph authentication configuration](https://docs.ceph.com/en/latest/rados/configuration/auth-config-ref/)
- [Ceph user and capability management](https://docs.ceph.com/en/latest/rados/operations/user-management/)
- [Ceph RBD encryption](https://docs.ceph.com/en/latest/rbd/rbd-encryption/)
- [Ceph OSD encryption](https://docs.ceph.com/en/latest/ceph-volume/lvm/encryption/)
- [Proxmox VE Administration Guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)
- [Proxmox Backup Client encryption](https://pbs.proxmox.com/docs/backup-client.html)
