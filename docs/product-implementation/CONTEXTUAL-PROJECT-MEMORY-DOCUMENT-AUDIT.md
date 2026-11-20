# Contextual Project Memory — Pre-Implementation Document Audit

**Date:** 2026-11-20\
**Scope:** documentation-only\
**Status:** READY FOR SM-CTX-001

Esta auditoria não executou nenhum Slice, alterou runtime ou verificou uma
implementação contextual. READY significa apenas baseline documental suficiente
para iniciar o handoff CURRENT em uma tarefa posterior.

## Sources reviewed

- `AGENTS.md` e o estado documental prévio de `CLAUDE.md`.
- Todos os artefatos `01–08` em `docs/product-implementation/`.
- Todos os handoffs `09–16` em `docs/product-implementation/implementation/`.
- ADR-0002, ADR-0012, ADR-0015 e ADR-0016; seções pertinentes de ADR-0009
  (idempotência/Forget) e ADR-0010 (destructive boundaries/retention).
- ADR-0017, ADR-0018 e ADR-0019 no documento `07`, sem renumeração.
- Evidência read-only: Cognitive Memory service/domain/ledger, `/info`, boundaries
  de Knowledge Forget e migrations existentes até `0018`. Não houve consulta ou
  alteração do estado de banco.

## Known findings

| Finding | Verification and correction | Result |
|---|---|---|
| 1 — Checkpoint tombstone stream identity | `10` permitia scrub ambíguo de scope/context_key. Preservação estrutural obrigatória em `05`/`10`, incluindo MAX+1, constraints, restart, testes e ACs. | Resolved |
| 2 — current_state input/result asymmetry | `10` não separava cardinalidade de mutation/result; `13` já permitia remoção por propagation. Inputs continuam obrigatórios; results aceitam estados legitimamente scrubbed. | Resolved |
| 3 — Resolved dependency wording | `11` ainda mandava corrigir `10`. Agora verifica a capability já aprovada/entregue por CTX-002, sem reexecutar o Slice. | Resolved |
| 4 — Evidence project isolation | `12` não fechava matching explícito de projeto. Episode/project-Memory Evidence exige mesmo projeto; global MemoryItem apenas por ref explícita; sem ownership inventado para SessionEntry/Knowledge. | Resolved |
| 5 — Recall with insufficient support | ACTIVE persistido não implica eligibility atual. `14` distingue ordinary invalidation de post-Forget invariant failure; `15`/`16` e Contract mantêm read-only, warnings e histórico seguro. | Resolved |
| 6 — Contract capability strings | `05` continha exemplos antigos e `16` definia nomes. Amendment de `05` congela as cinco strings; `16` implementa/referencia o Contract. | Resolved |

## Additional findings

### IMPORTANT — Resolved

- **Required query:** `05` §39 exige query; `14–16` tinham cenários públicos sem
  query. O usuário decidiu explicitamente nesta auditoria manter query obrigatória.
  Handoffs, ACs e validações foram alinhados; seleção sem embedding é fallback
  interno, sem novo request público.
- **Public names:** corrigidos request `support.refs` versus result
  `support.evidence`, `INVALID_EVIDENCE_CYCLE`, `knowledge_references`,
  `degraded_sources` e warnings já definidos no Contract. `include_evidence=false`
  não apaga refs/availability do recurso retornado.
- **Recovery Create:** `05`, `06` e `08` alinhados à emenda já APPROVED em `10`,
  incluindo histórico sem ACTIVE e ausência de lineage implícita.
- **Idempotency privacy:** `09` explicita a regra já aprovada em `06` §28 de lookup
  identity protegida, distinguindo-a do request HMAC. Não copiar raw-key retention
  do código cognitivo. `11` não admite replay de semantic snapshot esquecido;
  `13` exige convergence antes de successful Forget replay, inclusive outcomes
  do período incremental. Sem novo secret ou mudança do Contract cognitivo.

### MINOR — Resolved or bounded

- `08`: dependency de CTX-004 inclui CTX-003; RRF deferred é entre domínios e não
  proíbe RRF existente dentro de Knowledge; não há SessionEntry Forget hook.
- A numeração de ACs do backlog e dos handoffs é local a cada documento.
- `02`: status interno residual READY FOR REVIEW alinhado ao header APPROVED.
- Metadata hard breaks com trailing spaces foram convertidos para backslash Markdown, preservando apresentação e permitindo git diff --check limpo; corrigido um fragmento JSON conceitual sem braces em `12`.
- **Status preservado:** `16` continua READY FOR REVIEW, como encontrado. Sua
  futura execução exige aprovação explícita e autorização CURRENT; isso não
  bloqueia CTX-001 nem aprova antecipadamente CTX-008.
- Scope removido de Episode não é reconstruído para listing por projeto;
  tombstone permanece disponível por ID/replay, conforme Forget aprovado.

### BLOCKER

- None.

### NEEDS HUMAN DECISION

- None pending. A divergência de query foi decidida explicitamente pelo usuário.

## Operational gates

`AGENTS.md` agora explicita precedência por responsabilidade normativa, conflito
real sem regra clara como STOP/report, CURRENT + APPROVED como gate cumulativo e
handoff aprovado como execution plan suficiente para esta iniciativa.

PostgreSQL permanece autoridade; Contextual Project Memory v1 não usa Neo4j,
graph_outbox contextual, PipelineRun contextual ou LLM para Recall/support/Forget.
Embedding externo permanece fora da transaction. Knowledge Forget conserva seus
próprios pipelines/outbox e compartilha apenas o boundary PostgreSQL aprovado.

Nenhum Contract/capability contextual pode ser anunciado antes do activation gate
de SM-CTX-008. api_contract_version permanece "1". Evidence e propagation não são
capabilities independentes.

## Validation and Git scope

- Revisão cruzada de lifecycle, tombstones, revisions, idempotência, Evidence,
  isolamento, Forget, Recall, Knowledge, negotiation e migration expectations.
- Busca de aliases antigos, regras públicas sem query, instruções pendentes de
  corrigir CTX-002 e ambiguidades de scrub de identidade de checkpoint.
- Verificação documental de Markdown fences/JSON syntax e `git diff --check`.
- Nenhuma suíte Python, migration ou Human Validation de implementação executada:
  a tarefa é documentation-only e não demonstra funcionamento de runtime.
- O usuário autorizou incluir os 16 documentos previamente não versionados e as
  alterações prévias de AGENTS.md/CLAUDE.md no commit documental único.
  CLAUDE.md foi preservado sem edição nesta auditoria.

## Next executable handoff

`docs/product-implementation/implementation/09-SF-Implementation-SM-CTX-001-First-Class-Episodes.md`

Somente SM-CTX-001 — First-Class Episodes. Não executado nesta auditoria.
