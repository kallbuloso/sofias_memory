# Security Model

Sofias Memory is deliberately simple from an identity perspective: it is a **self-hosted, single-user service** protected by one static API key.

That simplicity is intentional. Sofias Memory is memory infrastructure, not an identity provider, tenant-management platform, or RBAC system.

---

## Authentication boundary

All protected `/api/v1/**` routes require:

```http
X-API-Key: <your-key>
```

The API key is validated with constant-time comparison.

The only always-public application routes are:

```text
/health/live
/health/ready
```

Swagger/OpenAPI documentation routes are only mounted in `dev` or `development`. Outside those environments, they do not exist.

A missing key and an invalid key produce stable public errors without exposing the configured credential.

---

## API key requirements

`API_KEY` must:

- begin with `sf-`;
- contain at least 32 random URL-safe characters after the prefix;
- not use the example placeholder;
- be treated as a secret;
- be different in each environment.

Generate it using a cryptographically secure random source. The repository includes `scripts/generate_api_key.py` for this purpose.

Do not:

- place the key in source code;
- commit it in `.env`;
- expose it to browser clients you do not fully control;
- reuse it as a database password or HMAC secret.

---

## Cognitive idempotency secret

Native Cognitive Memory uses a second secret:

```text
COGNITIVE_IDEMPOTENCY_HMAC_KEY
```

This secret is independent from `API_KEY`.

It protects the non-reversible request fingerprints stored in the Cognitive idempotency ledger. The ledger does not persist full Cognitive request bodies as plaintext.

Requirements:

- at least 32 characters;
- high entropy;
- never reuse `API_KEY`;
- never reuse across environments;
- do not log or return it through `/info` or errors.

This separation means rotating the HTTP credential does not automatically redefine existing idempotency history.

---

## Single-user means no tenancy boundary

Sofias Memory currently does **not** provide:

- user accounts;
- organizations;
- tenants;
- RBAC;
- per-user API keys;
- OAuth;
- JWT authentication;
- ACLs;
- public anonymous memory access.

Datasets, Cognitive scopes, Sessions, Skills, and Agent Profiles are application concepts, not security principals.

A Dataset is a logical namespace, not a tenant boundary.

A Cognitive scope such as `project:billing` is a recall/memory scope, not authorization.

If a multi-user application uses Sofias Memory, the caller is responsible for enforcing its own user/tenant permissions **before** calling the service.

---

## Network exposure

Recommended production posture:

```text
Internet / client
       │
       ▼
Reverse proxy / gateway with TLS
       │
       ▼
Sofias Memory API
       │
       ├── PostgreSQL + pgvector (private)
       └── Neo4j (private)
```

Do not expose PostgreSQL or Neo4j directly to the public internet.

Prefer:

- private Docker networks;
- internal VPC/VLAN connectivity;
- firewall rules or security groups;
- TLS termination at a trusted reverse proxy/load balancer;
- secret injection by the deployment platform.

---

## Swagger/OpenAPI exposure

The interactive documentation surface is allowlisted to:

```text
APP_ENV=dev
APP_ENV=development
```

Every other value — including the default `production` — disables the documentation routes.

This is fail-closed: an unexpected environment name does not accidentally expose Swagger.

The API itself remains available; only the documentation UI/schema surface is disabled.

---

## CORS

`CORS_ALLOWED_ORIGINS` defaults to empty.

Only add browser origins that must call Sofias Memory directly.

For server-to-server integrations such as backend services, n8n, workers, or internal automation, CORS is normally irrelevant and should remain empty.

Avoid wildcard browser exposure when the API key would be available to frontend code.

---

## Request size limits

`MAX_REQUEST_BODY_MB` defaults to `50`.

The request-body middleware rejects payloads larger than the configured limit before normal route processing proceeds.

Source-size handling also has its own `MAX_SOURCE_SIZE_MB` bound.

These limits reduce accidental memory pressure and oversized ingestion requests; they are not a substitute for a network-layer rate limit.

---

## Operational fail-closed behavior

During startup/migration/storage convergence, the process can remain alive while business traffic is not yet admitted.

This distinction is intentional:

- `/health/live` says the process exists;
- `/health/ready` says dependencies and the worker are operational;
- business-route admission is gated until bootstrap reaches an operational state.

Do not bypass readiness during deployment just because liveness is green.

See [Operations & Deployment](Operations-and-Deployment) and [Troubleshooting](Troubleshooting).

---

## Secret handling

Treat all of these as secrets:

```text
API_KEY
DATABASE_URL credentials
NEO4J_PASSWORD
COGNITIVE_IDEMPOTENCY_HMAC_KEY
LLM_API_KEY
EMBEDDING_API_KEY
STORAGE_S3_ACCESS_KEY_ID (when static)
STORAGE_S3_SECRET_ACCESS_KEY
STORAGE_S3_SESSION_TOKEN
```

Sofias Memory uses secret-aware configuration fields for sensitive values.

Production logs and public errors must not include:

- API keys;
- HMAC secrets;
- provider credentials;
- database connection strings with credentials;
- embeddings/vectors;
- raw SQL/constraint internals;
- provider exception payloads containing secrets.

---

## Content logging

The privacy defaults are intentionally conservative:

```text
LOG_DOCUMENT_CONTENT=false
LOG_LLM_PAYLOADS=false
```

Keep them false in normal production.

Enabling either option can cause sensitive application content to enter your logging infrastructure. If temporarily enabled for debugging:

1. use a controlled environment;
2. restrict log access;
3. set a short retention period;
4. disable the setting again after diagnosis;
5. rotate any secret that may have been captured.

`STORE_QUERY_CONTENT=true` is different: it controls durable Recall query content used by provenance/history features, not ordinary application logging.

---

## Destructive Forget and privacy

Native Cognitive Memory Forget is intentionally destructive.

When a Cognitive Memory is forgotten, the service scrubs the durable content-bearing fields and external provenance references while preserving the minimum tombstone identity and lineage required by the lifecycle contract.

Forgotten Cognitive Memory cannot be returned by current or historical Recall.

Knowledge Memory Forget has a separate Source/Dataset-oriented contract.

Read [Cognitive Memory](Cognitive-Memory), [Knowledge Memory](Knowledge-Memory), and [Provenance & Feedback](Provenance-and-Feedback) before designing privacy workflows.

---

## Object-storage permissions

When using S3, prefer least-privilege credentials scoped to the application-owned bucket/prefix.

Sofias Memory must be the exclusive writer within its configured prefix because deletion, convergence, and object-integrity behavior rely on deterministic ownership of that namespace.

If the platform can provide temporary role credentials, prefer the standard AWS credential provider chain over long-lived static keys.

See [Storage & S3](Storage-and-S3).

---

## Database security

PostgreSQL is the authoritative durable store and therefore contains the most sensitive application state.

Recommended controls:

- private network only;
- dedicated database/user;
- strong password or platform-managed identity where supported;
- encrypted storage at the infrastructure layer;
- encrypted backups;
- restricted backup access;
- regular restore tests.

Neo4j should receive similar network/credential protection even though it is a rebuildable projection.

---

## Error model

Public API errors use stable sanitized codes/messages. Examples include:

```text
MISSING_API_KEY
INVALID_API_KEY
INVALID_REQUEST
DEPENDENCY_UNAVAILABLE
IDEMPOTENCY_CONFLICT
MEMORY_STATE_CONFLICT
```

Use `request_id` for correlation rather than exposing internal stack traces to API clients.

---

## What Sofias Memory does not solve

The project intentionally does not attempt to provide:

- end-user authentication;
- authorization policy evaluation;
- network rate limiting;
- WAF/DDoS protection;
- secrets management;
- transport TLS termination;
- audit compliance certification;
- tenant isolation.

Those belong around Sofias Memory in the deployment/application architecture.

---

## Recommended production checklist

- [ ] strong unique `API_KEY`;
- [ ] independent `COGNITIVE_IDEMPOTENCY_HMAC_KEY`;
- [ ] TLS at ingress;
- [ ] PostgreSQL private;
- [ ] Neo4j private;
- [ ] production Swagger disabled;
- [ ] explicit CORS only if required;
- [ ] content/payload logging disabled;
- [ ] source storage backed up;
- [ ] PostgreSQL backed up and restore-tested;
- [ ] S3 credentials least-privilege;
- [ ] `/health/ready` used by orchestration;
- [ ] secrets never committed to Git.

See [Configuration Reference](Configuration-Reference), [Operations & Deployment](Operations-and-Deployment), and the canonical [Operations Guide](https://github.com/kallbuloso/sofias_memory/blob/main/docs/operations.md).
