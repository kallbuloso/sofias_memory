# ADR-0004: OpenAI-Compatible Only

## Status

accepted

## Context

Sofias Memory needs LLM and embedding capabilities, but the MVP should avoid a provider
matrix. Provider abstraction would add configuration branches, retry differences,
structured-output differences, error mapping differences, and test combinations before
the core memory system is stable.

The required integration point is an OpenAI-compatible API. This can be the OpenAI
service or a local endpoint that implements the required OpenAI-compatible behavior.

## Decision

Version 1 supports only OpenAI-compatible APIs for LLM and embeddings:

- configuration is supplied through the environment at startup;
- the implementation may use the OpenAI-compatible client path;
- a local OpenAI-compatible endpoint is allowed when configured through the
  environment and compatible with required features;
- no LiteLLM, Instructor, BAML, or provider router will be introduced in the MVP;
- no SDK or provider abstraction for Anthropic, Gemini, Mistral, Azure-specific APIs,
  or other non-OpenAI-compatible providers will be introduced in the MVP.

Application code must not accept per-request provider overrides.

## Consequences

The integration surface stays small and testable. Users who need local models can still
use an OpenAI-compatible local endpoint. Supporting non-compatible providers requires a
future explicit release decision and ADR.

## Alternatives Rejected

- LiteLLM, because it introduces a provider abstraction layer and extra behavior not
  needed for v1.
- Instructor or BAML, because structured output will be validated directly by project
  schemas and controlled repair behavior.
- Anthropic, Gemini, Mistral, Azure-specific, or other provider SDKs, because they
  expand the support matrix.
- Runtime provider selection by request, because configuration is immutable at startup.
- A generic provider registry, because the MVP has one integration protocol.

## References

- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 2.5, "Stack unica e sem opcionais".
- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 2.6, "Configuracao imutavel em runtime".
- `docs/product/Sofias_Memory_PRD_SPECS.md`, section 16.1, "Provider suportado".
- `AGENTS.md`, sections 4, 10, and 20.
