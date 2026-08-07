# ADR-0003: Single Static API Key

## Status

accepted

## Context

Sofias Memory is a single-user service. The MVP needs a simple access boundary for HTTP
requests without creating user identity, accounts, permissions, tenants, or API key
management workflows.

LLM and embedding credentials may also be needed, but those credentials configure
infrastructure providers. They are not user identity for Sofias Memory.

## Decision

The MVP will use one static application access key:

- clients authenticate with the `X-API-Key` header;
- the key is configured as `API_KEY=sf-...` in the environment;
- health endpoints are public;
- all other endpoints are private;
- the key is not persisted in PostgreSQL, Neo4j, files, logs, traces, metrics, or API
  responses;
- there is no CRUD API, rotation endpoint, key listing endpoint, user table, role
  model, permission model, ACL model, tenant model, or organization model;
- key comparison must use constant-time comparison;
- rotation is performed by changing the environment and restarting the application.

LLM and embedding credentials remain infrastructure configuration. They must not be
treated as Sofias Memory user identity and must not imply a provider credential
management surface.

## Consequences

The access model stays small and auditable. Operational key rotation is manual but
simple. Any future move to user accounts, multiple keys, or delegated access requires a
new accepted ADR and explicit product scope.

## Alternatives Rejected

- User authentication, login/register, JWT, cookies, OAuth, roles, permissions, ACLs,
  tenants, or organizations, because the MVP is single-user.
- Persisted API keys or API key management, because they create a new product surface
  and database responsibility.
- Key rotation endpoints, because rotation is handled by environment change and
  restart in v1.
- Passing keys through query strings, because headers are the only supported access
  mechanism.
- Treating LLM or embedding provider keys as Sofias Memory user identity.

## References

- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 2.2, "Single-user".
- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 2.3, "Autenticacao simplificada".
- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 2.4, "Credenciais do provedor de IA".
- `AGENTS.md`, sections 3 and 12.
