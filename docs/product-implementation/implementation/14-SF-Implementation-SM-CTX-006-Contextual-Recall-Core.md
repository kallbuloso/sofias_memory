# Sofias Memory — Implementation: SM-CTX-006 — Contextual Recall Core

**Document:** `implementation/14-SF-Implementation-SM-CTX-006-Contextual-Recall-Core.md`\
**Status:** APPROVED\
**Date:** 2026-11-20\
**Project:** Sofias Memory\
**Current Slice:** `SM-CTX-006 — Contextual Recall Core`

**Approved baseline:**
- `../01-SF-Discovery-Memoria-Contextual-Por-Projeto.md`
- `../02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md`
- `../03-SF-PRD-Memoria-Contextual-Por-Projeto.md`
- `../04-SF-Spec-Memoria-Contextual-Por-Projeto.md`
- `../05-SF-Contract-Memoria-Contextual-Por-Projeto.md`
- `../06-SF-Architecture-Memoria-Contextual-Por-Projeto.md`
- `../07-SF-ADRs-Memoria-Contextual-Por-Projeto.md`
- `../08-SF-Technical-Backlog-Memoria-Contextual-Por-Projeto.md`
- `09-SF-Implementation-SM-CTX-001-First-Class-Episodes.md`
- `10-SF-Implementation-SM-CTX-002-Context-Streams-Initial-Checkpoints.md`
- `11-SF-Implementation-SM-CTX-003-Checkpoint-Revision-Concurrency.md`
- `12-SF-Implementation-SM-CTX-004-First-Class-Evidence.md`
- `13-SF-Implementation-SM-CTX-005-Provenance-Aware-Forget.md`

---

# 1. Objective

Implementar:

```text
SM-CTX-006 — Contextual Recall Core
```

Este é o primeiro Slice que transforma os recursos implementados até aqui em uma experiência de continuidade consumível.

Ao final, um Consumer deverá conseguir perguntar ao Sofias Memory:

```text
Qual é o contexto atual deste projeto/stream?
```

ou:

```text
O que preciso recuperar para continuar este trabalho?
```

e receber um bundle estruturado combinando, de forma bounded e determinística:

```text
current ContextCheckpoint
+
current project-scoped Cognitive Memory
+
relevant ACTIVE Episodes
+
optional Evidence references
```

sem replay integral da conversa, sem LLM e sem executar qualquer ação.

---

# 2. Core Product Value

Até SM-CTX-005 o sistema consegue armazenar e proteger:

```text
what is known
what happened
where we are
why certain derived context exists
```

SM-CTX-006 torna esses elementos recuperáveis juntos.

Fluxo:

```text
Consumer
    ↓
POST /api/v1/context/recall
    ↓
resolve project + context stream
    ↓
current checkpoint
    +
current cognitive truth
    +
relevant historical Episodes
    ↓
bounded ProjectContextBundle
```

---

# 3. Current Role

```text
CURRENT ROLE: Senior Software Engineer
CURRENT PHASE: Implementation
CURRENT SLICE: SM-CTX-006
```

Não alterar decisões APPROVED.

---

# 4. Required Entry State

Antes de executar:

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
SM-CTX-004 DONE
SM-CTX-005 DONE
```

---

## 4.1 Expected capabilities

O repositório deve possuir:

```text
Episode persistence/retrieval
ContextCheckpoint/current lookup
ContextCheckpoint revisions
Evidence/support
Forget propagation
Cognitive Memory v1
```

---

# 5. Public Endpoint

Adicionar:

```http
POST /api/v1/context/recall
```

Esse endpoint é separado de:

```http
POST /api/v1/recall
```

e de:

```http
POST /api/v1/memories/recall
```

Os três possuem semânticas diferentes.

---

# 6. Recall Families Must Remain Distinct

## Knowledge Recall

```http
POST /api/v1/recall
```

responde:

```text
what does source-backed knowledge say?
```

---

## Cognitive Recall

```http
POST /api/v1/memories/recall
```

responde:

```text
which profile/semantic memories match this query?
```

---

## Contextual Recall

```http
POST /api/v1/context/recall
```

responde:

```text
what context should a Consumer have to understand or continue this project?
```

Não fundir os endpoints.

---

# 7. Contextual Recall Is Not RAG

SM-CTX-006 não gera answer textual.

Não chama:

```text
RagAnswerClient
LLM chat completion
```

Não produz:

```text
"answer": "..."
```

como Knowledge Recall.

Retorna contexto estruturado.

---

# 8. Embedding Is Allowed

Embedding pode ser usado para:

```text
semantic selection of Cognitive Memory
semantic selection of Episodes
```

Isso não transforma Contextual Recall em RAG.

---

# 9. No Agent Behavior

Contextual Recall não:

- decide próxima ação;
- executa `next_action`;
- escolhe ferramenta;
- cria Task;
- cria AgentRun;
- altera checkpoint;
- cria memória;
- promove Episode;
- resume workflow automaticamente.

O Consumer decide o que fazer com o bundle.

---

# 10. Read-Only Contract

`POST /context/recall` é semanticamente read-only.

MUST NOT criar:

```text
Query
PipelineRun
PipelineStep
Session
SessionEntry
MemoryItem
Episode
ContextCheckpoint
graph_outbox
```

Nenhuma mutation de domínio.

---

# 11. No Query Audit Row

Knowledge Recall cria:

```text
queries
```

para seu próprio provenance/feedback contract.

Contextual Recall v1 não deve reutilizar essa tabela apenas porque possui uma `query`.

Não criar `Query` row.

---

# 12. Project Scope

Request exige:

```text
scope = project:<key>
```

Não aceitar:

```text
global
```

como Contextual Recall scope.

---

# 13. Context Stream

`context_key` seleciona o Context Stream.

Quando omitido:

```text
context_key = main
```

A response deve retornar o valor resolvido explicitamente.

---

# 14. Episode Context Semantics

Episodes podem ter:

```text
context_key = requested context
```

ou:

```text
context_key = NULL
```

Episode com `context_key=NULL` é:

```text
project-wide
```

Logo, ao recuperar contexto para:

```text
(project:x, feature:a)
```

Episodes elegíveis são:

```text
scope = project:x

AND

(
    context_key = feature:a
    OR
    context_key IS NULL
)
```

Não incluir Episode de outro Context Stream específico.

---

# 15. Cognitive Memory Scope

Automatic Cognitive inclusion usa exclusivamente:

```text
scope = requested project scope
```

Exemplo:

```text
project:sofias-memory
```

MUST NOT incluir automaticamente:

```text
global
```

---

# 16. No Global Cognitive Auto-Inclusion

Esta regra é explícita.

Mesmo que global memories sejam semanticamente relevantes:

```text
global Cognitive Memory
```

não entra no bundle automaticamente.

O Consumer pode consultar Cognitive Memory separadamente se desejar.

---

# 17. Why Global Is Excluded

Contextual Recall deve ser previsível e project-isolated.

Incluir global automaticamente poderia misturar:

- preferências gerais;
- contexto pessoal;
- fatos cross-project;

em uma operação cujo scope foi explicitamente:

```text
project:<key>
```

---

# 18. Recall Intents

Contract v1 define:

```text
resume
context
explain
history
```

---

# 19. Intents Implemented in SM-CTX-006

Implementar funcionalmente:

```text
resume
context
```

---

# 20. Intents Deferred to SM-CTX-007

Ainda não implementar:

```text
explain
history
```

Esses dependem de:

- historical checkpoint composition;
- causal presentation;
- Knowledge integration.

---

# 21. Unsupported Intent During Partial Contract

Enquanto SM-CTX-007 não estiver implementado:

```text
intent=explain
intent=history
```

deve falhar explicitamente.

Nunca:

- ignorar intent;
- tratar como `context`;
- retornar resposta parcial fingindo suporte.

Usar erro compatível com o Contract existente, preferencialmente:

```text
INVALID_REQUEST
```

com mensagem segura indicando que o intent ainda não está disponível na implementação parcial.

O Contract completo ainda não está anunciado.

---

# 22. Request Shape

Usar o shape público exato definido pelo Contract APPROVED.

Conceitualmente:

```json
{
  "scope": "project:sofias-memory",
  "context_key": "feature:contextual-memory",
  "intent": "resume",
  "query": "Where did we stop?",
  "datasets": [],
  "include_evidence": true,
  "limits": {}
}
```

Não inventar novos campos públicos se o Contract já especifica o shape.

---

# 23. `query` — Required Public Input

`scope`, `intent` e `query` são obrigatórios no Contract `05` §39. O request HTTP MUST rejeitar query ausente/null ou inválida com `422 INVALID_REQUEST`. A revisão de 2026-11-20 confirmou explicitamente a manutenção desse contrato; seleção determinística sem embedding é fallback interno, não request público sem query.

---

# 24. Deterministic Retrieval Is an Internal Fallback

Current checkpoint é lookup direto. Seleção determinística bounded de memórias/Episodes não exige provider e permanece disponível internamente quando query embedding falha. Isso permite degradação segura de um request público válido, que continua contendo query.

---

# 25. `datasets`

O Contract já reserva:

```text
datasets
```

para Knowledge integration.

SM-CTX-006 ainda não implementa Knowledge composition.

Portanto:

```text
datasets absent/empty
    => valid
```

```text
datasets non-empty
    => explicit unsupported-partial-contract error
```

Não ignorar datasets silenciosamente.

SM-CTX-007 implementará seu significado.

---

# 26. `include_evidence`

Quando:

```text
include_evidence = false
```

preservar Evidence refs/availability nos recursos retornados, sem expansão textual completa. A seção agregada de expansão pode permanecer vazia conforme Contract; não apagar support/provenance.

Quando:

```text
true
```

retornar Evidence identities/availability permitidas pelo Contract.

Não retornar source content integral apenas porque Evidence foi solicitada.

---

# 27. Limits

Usar exatamente o limits shape aprovado no Contract.

Toda collection deve possuir:

- caller limit bounded;
- server hard maximum.

Não permitir unbounded recall.

---

# 28. ProjectContextBundle

Response é um bundle estruturado.

Shape conceitual:

```text
ProjectContextBundle

scope
context_key
intent
continuity_available

current_checkpoint
current_memories[]
episodes[]

historical_checkpoints[]
knowledge_references[]
evidence[]

warnings[]
degraded_sources
```

Usar os nomes exatos do Contract quando divergirem desta representação conceitual.

---

# 29. CTX-006 Bundle Population

Neste Slice:

```text
current_checkpoint
    implemented

current_memories
    implemented

episodes
    implemented

evidence
    implemented when requested
```

Enquanto:

```text
historical_checkpoints
knowledge_references
```

permanecem vazios/não populados conforme Contract.

---

# 30. `continuity_available`

Para `resume`:

```text
current checkpoint exists and current_state is recall-eligible
    => continuity_available = true
```

```text
no current checkpoint
    => continuity_available = false
```

---

# 31. Missing Current Checkpoint Is Normal

Não retornar:

```text
500
```

nem necessariamente:

```text
404
```

para Contextual Recall.

Ausência de current checkpoint é estado válido do Context Stream.

Response deve continuar sendo um bundle.

---

# 32. Missing Current Warning

Sem current checkpoint elegível, retornar `continuity_available=false`, `current_checkpoint=null` e warning público `NO_CURRENT_CHECKPOINT`. Não criar warning alternativo como alias.

---

# 33. No Historical Fallback

Mesmo durante Recall:

```text
current checkpoint absent
```

MUST NOT resultar em:

```text
return latest SUPERSEDED as current
```

Nem:

```text
return INVALIDATED revision as current
```

---

# 34. Missing Current Is Not Dependency Degradation

Ausência normal de continuidade não é dependency failure. Se os demais componentes estão saudáveis: `continuity_available=false`, `degraded_sources=[]`, `warnings=["NO_CURRENT_CHECKPOINT"]`.

---

# 35. `resume` Intent

Objetivo:

> recuperar o estado necessário para continuar o Context Stream.

Prioridade conceitual:

```text
1. current checkpoint
2. current project Cognitive Memory
3. relevant/recent ACTIVE Episodes
4. Evidence references when requested
```

---

# 36. `context` Intent

Objetivo:

> recuperar contexto contextual relevante ao assunto solicitado dentro do projeto.

Prioridade:

```text
1. current checkpoint
2. query-relevant current project Cognitive Memory
3. query-relevant ACTIVE Episodes
4. Evidence references
```

---

# 37. `resume` and `context` Need Not Be Identical

`resume` privilegia:

```text
continuity
recency
current state
```

`context` privilegia:

```text
semantic relevance to query
```

Ambos continuam bounded.

---

# 38. Current Checkpoint Retrieval

Lookup:

```text
(scope, resolved context_key, lifecycle=ACTIVE)
```

Exatamente um ou nenhum.

Não usar embedding.

---

# 39. Checkpoint Is Highest-Priority Continuity Source

Em `resume`, nenhum MemoryItem ou Episode substitui o checkpoint.

Eles complementam.

---

# 40. Current Checkpoint Content

Hydrate:

```text
objective
current_state
approved_decisions
pending_work
open_items
blockers
next_action
```

somente statements ainda existentes após Forget propagation.

---

# 41. No Execution Semantics for `next_action`

Retornar:

```text
next_action
```

como context.

Nunca interpretá-lo como comando.

---

# 42. Cognitive Memory Eligibility

Selecionar apenas:

```text
memory_type = profile | semantic
scope = requested project scope
current truth
not forgotten
```

Não incluir superseded por default.

---

# 43. Cognitive Temporal Semantics

Usar as regras já estabilizadas pelo Native Cognitive Memory.

Não criar segundo conceito de “current truth”.

---

# 44. Current Memory Selection With Query

Para query válida do request público:

```text
query embedding when available
    ↓
exact pgvector cosine
    ↓
current project Cognitive Memory
```

Reutilizar ranking semantics já aprovadas.

---

# 45. Current Memory Selection in Deterministic Fallback

No fallback interno após falha de query embedding, selecionar memórias current do projeto de forma determinística e bounded, por recência/identidade coerentes com o domínio. Não retornar todas as project memories. O request HTTP original possui query.

---

# 46. No Global Fallback

Se o projeto não possuir Cognitive Memory:

```text
current_memories = []
```

Não buscar `global`.

---

# 47. Episode Eligibility

Normal Contextual Recall inclui somente:

```text
lifecycle = ACTIVE
```

Nunca incluir:

```text
INVALIDATED
FORGOTTEN
```

no core bundle.

---

# 48. Historical Episode Semantics

Episode ACTIVE continua histórico por natureza.

Ele não vira current truth apenas por estar no bundle.

Response shape deve preservar claramente que:

```text
Episode != MemoryItem
```

---

# 49. Episode Selection With Query

Para query válida do request público:

```text
query embedding when available
    ↓
exact pgvector cosine
    ↓
ACTIVE Episodes
    ↓
scope/context eligibility
```

Ranking:

```text
relevance DESC
occurred_at DESC
episode_id ASC
```

conforme CTX-001.

---

# 50. Episode Selection in Deterministic Fallback

Após falha de query embedding, usar bounded recency: `occurred_at DESC, episode_id ASC`, respeitando scope/context, lifecycle e support eligibility. Não fabricar relevance.

---

# 51. Project-Wide Episodes

Sempre podem ser candidatos dentro do requested project:

```text
context_key IS NULL
```

---

# 52. Context-Specific Episodes

Também candidatos quando:

```text
episode.context_key = requested context_key
```

---

# 53. Other Context Streams

Episode:

```text
context_key = feature:b
```

MUST NOT entrar automaticamente em Recall de:

```text
context_key = feature:a
```

mesmo que semanticamente semelhante.

---

# 54. Cross-Project Isolation

Nenhum root contextual ou current project MemoryItem de:

```text
project:b
```

pode aparecer em bundle de:

```text
project:a
```

independentemente de similarity. Evidence global explicitamente referenciada segue CTX-004; SessionEntry/Knowledge não adquire project ownership.

---

# 55. Single Query Embedding

Quando semantic retrieval for necessário, gerar no máximo:

```text
one query embedding
```

por Contextual Recall request.

Reutilizar para:

```text
Cognitive Memory retrieval
Episode retrieval
```

Não chamar provider duas vezes para o mesmo query.

---

# 56. Embedding Boundary

Não manter transaction PostgreSQL aberta durante provider call.

Fluxo:

```text
resolve deterministic state in short read
close UoW

embed query

open read UoW
perform semantic retrieval
close UoW

compose bundle
```

ou ordem equivalente sem transaction longa.

---

# 57. Fallback Does Not Retry Embedding

O caminho determinístico interno não chama provider. Depois de uma tentativa de embedding malsucedida, não fazer chamadas adicionais para obter esse fallback. Query ausente é rejeitada na validação HTTP.

---

# 58. Semantic Provider Failure

Architecture aprovada exige degraded read model.

Se query embedding falhar:

```text
Contextual Recall SHOULD still return deterministic context
```

quando PostgreSQL continua disponível.

---

# 59. Degraded Semantic Recall

Em embedding failure, usar `warnings=["PARTIAL_CONTEXT"]` e `degraded_sources` identificando as fontes sem recuperação semântica. Retornar current checkpoint elegível e memórias/Episodes por fallback determinístico bounded quando PostgreSQL continua disponível. Não expor campo público `degraded` nem inventar warning fora de `05` §51.

---

# 60. No Silent Semantic Fallback

Se semantic retrieval falhou:

```text
warning MUST say so
```

Não apresentar recency fallback como se fosse semantic ranking.

---

# 61. PostgreSQL Failure

PostgreSQL é autoridade de todo o bundle.

Database failure:

```text
request fails
```

Não retornar bundle fabricado.

---

# 62. Evidence Resolution Failure

Uma dependency operacional inesperada durante Evidence resolution não deve ser convertida em:

```text
Evidence unavailable
```

Se o core bundle pode ser retornado com Evidence expansion omitida de forma Contract-compatible:

```text
degraded_sources identifies affected sources; warning=PARTIAL_CONTEXT
warning
```

pode ser aceitável.

Caso contrário:

```text
dependency failure
```

Fail closed onde necessário.

Não persistir availability falsa.

---

# 63. Persistence Lifecycle vs Recall Eligibility

`persistence lifecycle != recall eligibility` quando support explícito estiver atualmente insuficiente.

Exemplo: B permanece ACTIVE com `all_required [Episode A]`, mas A foi invalidado e agora é UNAVAILABLE. Ordinary invalidation não dispara destructive propagation de CTX-005. Esse estado não é, por si só, corrupção.

- `support=null` não é unsupported: nenhuma dependency explícita foi declarada.
- Direct GET retorna estado persistido real e Evidence availability atual, sem mutação.
- `resume`/`context` excluem Episodes unsupported e statements unsupported do contexto utilizável. Podem retornar os outros statements elegíveis do checkpoint.
- Se `current_state` não estiver suportado/elegível, retornar `current_checkpoint=null`, `continuity_available=false`, sem histórico como fallback. O row pode continuar fisicamente ACTIVE.
- Emitir `EVIDENCE_UNAVAILABLE` e `PARTIAL_CONTEXT`, além de `NO_CURRENT_CHECKPOINT` quando continuidade não estiver disponível. Não inventar public support-status field.
- Em SM-CTX-007, `history` pode mostrar recurso histórico elegível com lifecycle e Evidence availability explícitos, sem current-truth claim.
- Após successful Forget, conteúdo que CTX-005 deveria ter scrubbed é invariant failure: fail closed, não expor semantic content e não reparar durante read.

Revalidar eligibility mesmo com `include_evidence=false`; a flag controla expansão, não segurança de leitura.

---

# 64. No Lifecycle Mutation on Recall

Se Read detectar incoerência:

```text
do not mutate lifecycle
```

Recall é read-only.

Uma inconsistency impossível segundo CTX-005 deve:

- degradar/falhar conforme segurança;
- ser observável;
- nunca ser “consertada” durante GET/Recall.

---

## 64.1 Required Support Eligibility Tests

Usar PostgreSQL real: criar B ACTIVE com `all_required [A]`, invalidar A e confirmar que B continua fisicamente ACTIVE. Direct GET mostra availability atual; resume/context não expõem B como contexto utilizável e não alteram rows. Repetir com checkpoint cujo current_state depende de A: omitir current_checkpoint, continuity_available=false e warnings estáveis, sem reativação histórica. Com any_sufficient e outra Evidence AVAILABLE, preservar eligibility. Após successful Forget, injetar invariant failure em fixture isolada e provar que nenhum conteúdo que deveria estar scrubbed é exposto. Cobrir os mesmos estados com include_evidence=false.

---

# 65. Evidence Expansion

Quando requested:

```text
Episode support
ContextStatement support
```

pode alimentar seção agregada:

```text
evidence[]
```

do bundle.

---

# 66. Evidence Deduplication

Se várias unidades referenciam:

```text
("episode", X)
```

retornar uma Evidence identity canônica na seção agregada quando o Contract assim modelar.

Não duplicar conteúdo.

---

# 67. No Evidence Content Expansion

EvidenceRef pode retornar:

```text
source_type
source_id
availability
```

e metadados permitidos pelo Contract.

Não retornar automaticamente:

- SessionEntry content;
- MemoryItem content fora de `current_memories`;
- source document text;
- Chunk text.

---

# 68. SessionEntry Evidence

Pode aparecer como:

```text
EvidenceRef
```

quando sustentando Episode/statement.

SM-CTX-006 não faz:

```text
full Session replay
```

---

# 69. No Session Creation

Contextual Recall não possui o comportamento de lazy Session creation do Knowledge Recall.

Não reutilizar:

```text
RecallService._admit_session()
```

---

# 70. No Session Context Injection

Não existe:

```text
include_session_context
```

implícito.

SessionEntries entram apenas conforme o Contract contextual permitir, especialmente como Evidence reference.

---

# 71. Knowledge Integration Deferred

SM-CTX-006 não chama:

```text
RecallService
```

para Knowledge retrieval.

Não executa:

- vector chunk search;
- lexical search;
- RRF;
- graph recall;
- summaries;
- RAG.

---

# 72. Why Knowledge Is Deferred

`SM-CTX-007` é responsável por:

```text
Explain
History
Knowledge Integration
```

Misturar Knowledge agora reduziria a clareza do core contextual e aumentaria dependências antes da validação da continuidade básica.

---

# 73. Neo4j Independence

Contextual Recall Core funciona com Neo4j indisponível.

Nenhuma chamada ao graph.

---

# 74. No `graph_outbox`

Read-only.

Zero new graph events.

---

# 75. No `PipelineRun`

Recall é síncrono.

Zero PipelineRuns.

---

# 76. No `Query`

Zero Knowledge Query audit rows.

---

# 77. No New Persistence Table

Preferência:

```text
no migration
```

SM-CTX-006 é principalmente service/schema/API/repository read behavior.

Criar table de Contextual Recall audit está fora de escopo.

---

# 78. Contextual Recall Service

Criar:

```text
ContextualRecallService
```

Responsabilidades:

```text
validate partial-contract capability
resolve context stream
retrieve current checkpoint
select Cognitive Memory
select Episodes
resolve optional Evidence
apply per-source limits
compose ProjectContextBundle
emit Contract warnings/degraded_sources
```

---

# 79. Do Not Extend Knowledge Recall Service

Não colocar Contextual Recall dentro de:

```text
services/recall.py
```

Esse service possui:

- Knowledge semantics;
- Query audit;
- graph;
- RAG;
- Session admission.

Contextual Recall é domínio diferente.

---

# 80. Reuse Without Coupling

Reutilizar primitives existentes quando apropriado:

```text
EmbeddingClient protocol
Cognitive repository ranking
Episode repository ranking
EvidenceResolver
checkpoint repositories
```

Não duplicar algoritmos de pgvector.

---

# 81. Repository Additions

Possíveis necessidades:

```text
MemoryItemRepository.list_current_for_scope(...)
EpisodeRepository.list_recent_active(...)
ContextCheckpointRepository.get_current(...)
```

Se query presente, reutilizar:

```text
typed_recall / semantic search
```

ou equivalente coerente.

---

# 82. No New Generic Retrieval Framework

Não criar:

```text
GenericMemoryRetriever
UniversalRecallEngine
RetrievalProviderRegistry
ContextSourcePlugin
```

Apenas três first-party sources neste Slice:

```text
Checkpoint
Cognitive Memory
Episode
```

---

# 83. Source-Specific Ranking

Não misturar scores heterogêneos em um único ranking global.

Checkpoint não possui similarity.

Cognitive Memory e Episode relevance podem ter distribuição diferente.

Manter collections separadas.

---

# 84. No Cross-Type Score Competition

Não fazer:

```text
Memory relevance 0.81
Episode relevance 0.79
=> global rank
```

Isso cria comparabilidade não aprovada.

---

# 85. Per-Source Limits

Aplicar limits independentemente.

Exemplo conceitual:

```text
checkpoint: max 1
current_memories: bounded N
episodes: bounded M
```

Não deixar Episodes consumir orçamento de checkpoint.

---

# 86. Overall Response Bound

Além de source limits, respeitar máximo operacional de response.

Se o Contract possui budget explícito:

```text
use it
```

Se não, utilizar hard caps existentes/locais sem criar novo public Contract.

---

# 87. Determinism

Mesmo request + mesmo database state + mesmo embedding result deve produzir:

```text
same ordering
same selected identities
```

Ties precisam de UUID/timestamp stable ordering.

---

# 88. Warning Ordering

Warnings também devem ser determinísticos.

Não depender da ordem de exceptions/async tasks.

---

# 89. No Async Race in Composition

É permitido executar independent reads concorrentemente se:

- cada um usa UoW/session segura própria;
- ordering final é deterministicamente normalizado;
- não aumenta complexidade desnecessária.

Sequencial simples é aceitável.

---

# 90. Response Memory Shape

Preferir reutilizar:

```text
MemoryItemResult
```

ou wrapper contextual contendo:

```text
memory
relevance?
```

Não inventar nova representação incompatível do mesmo MemoryItem.

---

# 91. Response Episode Shape

Reutilizar lifecycle-safe Episode result produzido por CTX-001/004.

Pode envolver wrapper:

```text
episode
relevance?
```

se previsto pelo Contract.

---

# 92. Non-Semantic Selection Score

Quando não houve semantic query:

```text
do not fabricate relevance
```

Não retornar:

```text
relevance = 1.0
```

apenas para satisfazer schema.

O shape deve permitir ausência de relevance se o Contract assim prevê.

---

# 93. Required Query Validation

Request público `resume` sem query ou com query null MUST falhar com `422 INVALID_REQUEST`, antes de provider/retrieval. Fallback interno sem embedding não altera esse requisito.

---

# 94. `resume` With Query

Fluxo:

```text
current checkpoint

one query embedding

semantic current project memories

semantic eligible ACTIVE Episodes

optional Evidence
```

Checkpoint continua prioridade de continuidade.

---

# 95. `context` With Query

Mesmo retrieval primitives, mas o Consumer intent comunica que:

```text
semantic relevance
```

é o foco, não apenas continuidade.

Não gerar resposta textual.

---

# 96. `context` Query Validation

`context` também exige query no HTTP. Não sintetizar query a partir de current_state. Seleção determinística existe apenas internamente, inclusive no fallback por provider failure.

---

# 97. Do Not Embed Checkpoint Text

Checkpoint não possui embedding no MVP.

Não montar texto e chamar provider apenas para rankear checkpoint.

Existe zero ou um current.

---

# 98. Do Not Create Checkpoint Embedding

Continua DEFERRED.

---

# 99. Forget Safety

Normal recall MUST NOT retornar semantic content de:

```text
forgotten MemoryItem
forgotten Episode
forgotten ContextStatement
forgotten Checkpoint payload
```

---

# 100. Invalidated Episode

Normal Contextual Recall exclui.

Mesmo para history:

```text
history not implemented yet
```

---

# 101. Superseded Cognitive Memory

Normal current bundle exclui.

Historical Cognitive retrieval não pertence a CTX-006.

---

# 102. Superseded Checkpoint

Não entra como current.

Historical checkpoint section permanece vazia até CTX-007.

---

# 103. Current Checkpoint Invalidated by Forget Propagation

Se CTX-005 removeu `current_state` e invalidou checkpoint:

```text
continuity_available=false
```

Não recuperar statements remanescentes daquele checkpoint como `current_checkpoint`.

---

# 104. Ambiguity

Se invariants do banco forem violados e aparecerem dois ACTIVE checkpoints:

```text
fail closed
```

Não escolher:

```text
newest wins
```

Isso indicaria corrupção/invariant failure.

---

# 105. Missing Context Is Normal

Pode existir:

```text
no checkpoint
no current project memories
no active episodes
```

Ainda assim response válida:

```text
empty contextual bundle
```

com warning apropriado para resume.

---

# 106. Public Degradation Representation

O bundle público usa `degraded_sources`, conforme `05` §49, e warnings estáveis do §51. Um indicador interno de degradação pode existir, mas não cria campo público `degraded`. Ausência de dados, falta normal de checkpoint e truncation saudável não são falhas de dependency.

---

# 107. Examples

- Projeto vazio saudável: `continuity_available=false`, `degraded_sources=[]`, warning `NO_CURRENT_CHECKPOINT`.
- Provider de embedding indisponível: `PARTIAL_CONTEXT`, fontes afetadas em `degraded_sources`, fallback determinístico seguro.
- Nenhum Episode relevante: `episodes=[]`, sem degradação por ausência de dados.

---

# 108. Error Codes

Reutilizar Contract-approved errors.

Conceitualmente necessários:

```text
INVALID_REQUEST
INVALID_SCOPE
INVALID_CONTEXT_KEY
DEPENDENCY_UNAVAILABLE
```

Partial-contract intent/datasets rejection deve usar stable existing error semantics.

Não criar erros excessivamente específicos sem necessidade.

---

# 109. Route

Criar:

```text
sofias_memory/api/routes/context_recall.py
```

ou organização equivalente coerente.

Route deve:

- validar transport;
- construir service;
- chamar service;
- retornar envelope.

Não conter retrieval orchestration.

---

# 110. Schemas

Adicionar schemas contextuais de recall.

Exemplos conceituais:

```text
ContextRecallRequest
ContextRecallLimits
ContextRecallMemoryItem
ContextRecallEpisodeItem
ContextRecallWarning
ProjectContextBundle
```

Usar nomes do Contract quando já definidos.

---

# 111. API Path Registration

Registrar:

```http
POST /api/v1/context/recall
```

Nenhum outro endpoint novo neste Slice.

---

# 112. Contract Advertisement

Ainda MUST NOT anunciar:

```text
contextual_project_memory = "1"
```

em `/api/v1/info`.

Ainda faltam:

```text
SM-CTX-007
SM-CTX-008
```

---

# 113. Partial Capability Advertisement

Não inventar capability pública temporária como:

```text
contextual_project_memory.partial
```

a menos que já esteja prevista no baseline.

O gate continua sendo:

```text
full contract advertised only at CTX-008
```

---

# 114. OpenAPI

Endpoint deve aparecer.

Contract test deve garantir:

- POST only;
- request/response shape;
- no contextual management endpoints extras;
- no Evidence management endpoint;
- no PATCH.

---

# 115. Unit Tests — Scope

Validar:

```text
project:a accepted
global rejected
```

---

# 116. Unit Tests — Context Key

Validar:

- explicit context;
- omitted -> main;
- invalid grammar rejected.

---

# 117. Unit Tests — Episode Eligibility

Dataset:

```text
Episode A project:x context=NULL
Episode B project:x context=main
Episode C project:x context=feature:y
Episode D project:y context=main
```

Recall:

```text
project:x/main
```

deve incluir candidatos:

```text
A
B
```

e nunca:

```text
C
D
```

---

# 118. Unit Tests — Global Cognitive Exclusion

Seed:

```text
global Memory G
project:x Memory P
```

Recall project:x.

Expected:

```text
P eligible
G excluded
```

---

# 119. Unit Tests — Intent Gating

CTX-006:

```text
resume -> supported
context -> supported
explain -> explicit failure
history -> explicit failure
```

---

# 120. Unit Tests — Dataset Gating

```text
datasets=[]
    -> valid

datasets=["docs"]
    -> explicit unsupported-partial-contract failure
```

Não silently ignore.

---

# 121. Unit Tests — Required Query

HTTP requests sem query/null são rejeitados com `422 INVALID_REQUEST` e zero provider calls. Testar separadamente o seletor determinístico interno: zero provider calls adicionais.

---

# 122. Unit Tests — Single Embedding

Recall com query.

Assert:

```text
embedding_client.calls == 1
```

mesmo recuperando:

```text
MemoryItems + Episodes
```

---

# 123. Unit Tests — Ranking

Memory ordering deterministic.

Episode ordering:

```text
relevance DESC
occurred_at DESC
episode_id ASC
```

---

# 124. Unit Tests — Degraded Embedding

Provider fails.

Expected:

```text
200 bundle
degraded_sources identifies affected sources; warning=PARTIAL_CONTEXT
warning present
current checkpoint preserved
```

mais deterministic fallback permitido pelo Contract.

---

# 125. PostgreSQL Integration — Resume

Seed:

```text
project:x/main current checkpoint

project:x current memories

project:x project-wide Episodes

project:x/main Episodes

project:x/other Episodes
```

Recall:

```text
scope=project:x
context_key=main
intent=resume
```

Confirmar boundaries.

---

# 126. PostgreSQL Integration — Context

Com embeddings controláveis, validar:

```text
query-relevant MemoryItems
query-relevant Episodes
```

e source-specific limits.

---

# 127. PostgreSQL Integration — Missing Current

Não criar checkpoint.

Recall `resume`.

Expected:

```text
continuity_available=false
warning present
other valid context may still be returned
```

---

# 128. PostgreSQL Integration — No Fallback

Seed:

```text
rev1 SUPERSEDED
rev2 INVALIDATED
no ACTIVE
```

Recall.

Expected:

```text
current_checkpoint = null
continuity_available=false
```

Nunca rev1.

---

# 129. PostgreSQL Integration — Forget Safety

Seed resources, depois executar Forget propagation.

Recall.

Confirmar que nenhum scrubbed resource volta.

---

# 130. PostgreSQL Integration — Evidence

Com:

```text
include_evidence=true
```

confirmar:

- identities;
- availability;
- dedupe;
- zero full source text.

Com:

```text
false
```

confirmar refs/availability nos recursos e ausência de expansão textual completa; seção agregada vazia é permitida.

---

# 131. PostgreSQL Integration — Project Isolation

Seed projetos A/B com conteúdo semanticamente semelhante.

Recall A.

Zero IDs de B.

---

# 132. PostgreSQL Integration — Restart

Seed context.

Restart app/service boundary.

Recall.

Mesmo current context.

---

# 133. Read-Only Integration Test

Antes/depois de Contextual Recall, contar:

```text
queries
pipeline_runs
pipeline_steps
session_entries
memory_items
episodes
context_checkpoints
graph_outbox
```

Recall não cria nenhuma row.

---

# 134. No Neo4j Test

Executar Contextual Recall com Neo4j resource ausente/indisponível no harness aplicável.

Core recall continua funcionando.

---

# 135. No LLM Test

Fake chat/RAG client deve registrar:

```text
0 calls
```

Idealmente ContextualRecallService nem depende dessa interface.

---

# 136. No Knowledge Recall Test

Fake/spy do Knowledge RecallService:

```text
0 calls
```

em CTX-006.

---

# 137. Context Budget Test

Seed mais candidates do que permitido.

Confirmar:

```text
bounded memories
bounded Episodes
one current checkpoint
```

Deterministic truncation.

---

# 138. Evidence Budget

`include_evidence=true` não pode produzir response unbounded através de fan-out.

Evidence expansion deve obedecer Contract/server caps.

---

# 139. Logging

Não logar:

- checkpoint statements;
- MemoryItem content;
- Episode content;
- query integral se logging policy já evita content;
- Evidence source content.

Pode logar:

```text
scope
context_key
intent
degraded
continuity_available
memory_count
episode_count
evidence_count
```

quando seguro.

---

# 140. Security

Tudo retornado por Contextual Recall continua:

```text
untrusted context
```

Nunca:

- system instruction;
- permission;
- execution grant;
- tool authorization.

---

# 141. Expected File Impact

Possíveis novos arquivos:

```text
sofias_memory/services/contextual_recall.py
sofias_memory/schemas/context_recall.py
sofias_memory/api/routes/context_recall.py
```

Alterações prováveis:

```text
EpisodeRepository
MemoryItemRepository
ContextCheckpointRepository
Evidence service/repository
app.py
tests
```

Preferência:

```text
no migration
```

---

# 142. Implementation Order

Ordem recomendada:

```text
1. verify CTX-001..005 completion
2. schemas / intent validation
3. current checkpoint retrieval
4. deterministic Episode fallback retrieval
5. deterministic Cognitive fallback retrieval
6. single embedding path
7. semantic Memory retrieval
8. semantic Episode retrieval
9. ProjectContextBundle composition
10. Evidence expansion
11. degraded provider behavior
12. partial-contract intent/dataset gates
13. route/OpenAPI
14. project isolation
15. read-only assertions
16. restart validation
17. Human Validation
```

---

# 143. Acceptance Criteria

## AC-006.1

Existe:

```http
POST /api/v1/context/recall
```

## AC-006.2

`resume` retorna current checkpoint quando existente.

## AC-006.3

Ausência de current retorna:

```text
continuity_available=false
```

sem historical fallback.

## AC-006.4

Bundle pode incluir current project-scoped Cognitive Memory.

## AC-006.5

Global Cognitive Memory não é incluída automaticamente.

## AC-006.6

Bundle pode incluir ACTIVE Episodes context-specific.

## AC-006.7

Bundle pode incluir project-wide Episodes (`context_key=NULL`).

## AC-006.8

Episodes de outro Context Stream específico são excluídos.

## AC-006.9

Resources de outro Project Scope são excluídos.

## AC-006.10

Recall público sem query é rejeitado; seletor determinístico interno não chama provider.

## AC-006.11

Recall com query gera no máximo um query embedding.

## AC-006.12

Mesmo embedding pode alimentar Cognitive + Episode semantic retrieval.

## AC-006.13

Contextual Recall não chama LLM.

## AC-006.14

Contextual Recall não cria Query/PipelineRun/SessionEntry.

## AC-006.15

Contextual Recall não usa Neo4j.

---

# 144. Additional Acceptance Criteria

## AC-006.16

`resume` e `context` são suportados.

## AC-006.17

`explain` e `history` falham explicitamente até CTX-007.

## AC-006.18

Non-empty `datasets` não é silenciosamente ignorado.

## AC-006.19

Embedding failure pode produzir bundle degradado quando PostgreSQL continua disponível.

## AC-006.20

Degraded response possui warning explícito.

## AC-006.21

Ausência de dados não é confundida com degraded dependency.

## AC-006.22

Normal recall nunca retorna forgotten semantic content.

## AC-006.23

Evidence expansion não retorna automaticamente source content integral.

## AC-006.24

Recall é bounded por source/overall limits.

## AC-006.25

Ordering é determinístico. Eligibility revalida support atual sem mutar lifecycle; ACTIVE unsupported é excluído de resume/context e post-Forget invariant failure não expõe conteúdo semântico.

## AC-006.26

Nenhum graph_outbox é criado.

## AC-006.27

Full Contextual Project Memory Contract ainda não é anunciado.

---

# 145. Human Validation

Executar contra API real.

## Step A — Seed project context

Criar:

```text
scope = project:contextual-validation
context_key = main
```

Adicionar:

- current checkpoint;
- project MemoryItems;
- global MemoryItem;
- project-wide Episode;
- main Episode;
- Episode em outro context_key.

---

## Step B — Required query and deterministic fallback

Primeiro enviar query omitida/null: esperar `422 INVALID_REQUEST`. Depois executar request válido:

```text
intent=resume
query="Onde paramos?"
```

Confirmar:

- checkpoint current;
- project memories;
- project-wide Episode;
- main Episode;
- global memory ausente;
- other-context Episode ausente;
- uma única tentativa de query embedding; fallback interno sem novas chamadas se o provider falhar.

---

## Step C — Context with query

Executar query semanticamente próxima de um Episode/Memory específico.

Confirmar ranking.

---

## Step D — Evidence

Repetir com:

```text
include_evidence=true
```

Confirmar Evidence refs.

---

## Step E — Missing continuity

Invalidar current checkpoint.

Recall resume.

Confirmar:

```text
continuity_available=false
```

e nenhum fallback.

---

## Step F — Embedding unavailable

Indisponibilizar provider de embedding.

Recall com query.

Confirmar:

```text
degraded_sources identifies affected sources; warning=PARTIAL_CONTEXT
warning
```

e current checkpoint ainda retornado.

---

## Step G — Forgotten content

Forget source/memory que sustenta derived context.

Executar Recall.

Confirmar ausência do semantic content scrubbed.

---

## Step H — Partial-contract gates

Testar:

```text
intent=history
intent=explain
datasets non-empty
```

Todos devem falhar explicitamente, nunca ser ignorados.

---

## Step I — Restart

Reiniciar Sofias Memory.

Repetir `resume`.

Confirmar continuidade equivalente.

---

## Step J — Read-only audit

Confirmar que Recall não criou:

```text
Query
PipelineRun
PipelineStep
SessionEntry
Episode
MemoryItem
Checkpoint
graph_outbox
```

---

# 146. Required Test Commands

Executar:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

mais suite PostgreSQL opt-in contextual recall.

Se nenhuma migration existir:

```text
report migration = none
```

Não inventar migration apenas para manter sequência numérica.

---

# 147. Findings Classification

## BLOCKER

Exemplos:

- global memory vaza automaticamente;
- outro project aparece no bundle;
- historical checkpoint vira current fallback;
- forgotten content reaparece;
- Recall cria domain mutations;
- query gera duas embeddings desnecessárias;
- provider failure derruba resume que poderia ser recuperado;
- datasets são silenciosamente ignorados;
- LLM é chamado.

## IMPORTANT

Afeta utilidade/correctness do core contextual.

## MINOR

Não impede objetivo.

## DEFERRED

Pertence a CTX-007/008 ou evolução posterior.

---

# 148. When to Stop and Escalate

Voltar para decisão humana se surgir necessidade de:

- incluir global Cognitive Memory automaticamente;
- alterar intent semantics;
- criar Contextual Recall audit table;
- chamar LLM;
- criar answer textual;
- integrar Knowledge antecipadamente;
- usar Neo4j;
- adicionar Session replay;
- criar checkpoint embeddings;
- alterar ProjectContextBundle Contract;
- alterar limits públicos;
- tornar Recall mutating.

---

# 149. Slice Exit Criteria

SM-CTX-006 estará pronto para closeout quando:

```text
AC-006.1..27 satisfied

resume usable
context usable
current continuity semantics proven
project isolation proven
global cognitive exclusion proven
project-wide Episode semantics proven
single embedding proven
required public query and zero-provider internal fallback proven
degraded provider behavior proven
Evidence expansion proven
read-only behavior proven
no LLM
no Knowledge retrieval
no Neo4j
no PipelineRun
no graph_outbox
no BLOCKER remains
```

---

# 150. Expected Implementation Report

O executor deverá retornar:

```text
SM-CTX-006 — Implementation Report

Implemented
- ...

Changed files
- ...

Database
- migration: none / ...
- schema changes:

Dependencies verified
- SM-CTX-001:
- SM-CTX-002:
- SM-CTX-003:
- SM-CTX-004:
- SM-CTX-005:

Recall composition
- current checkpoint:
- cognitive memory:
- episodes:
- evidence:
- query embedding:
- degraded behavior:

Verified
- unit:
- PostgreSQL integration:
- resume:
- context:
- missing current:
- project isolation:
- global cognitive exclusion:
- project-wide Episodes:
- semantic ranking:
- required query / internal fallback:
- single-query embedding:
- embedding degradation:
- Evidence expansion:
- forgotten-content exclusion:
- partial-contract gates:
- restart:
- read-only assertions:
- no Neo4j:
- no LLM:
- no Knowledge recall:
- graph isolation:
- pipeline isolation:

Acceptance Criteria
- AC-006.1 PASS/FAIL
...
- AC-006.27 PASS/FAIL

Findings

BLOCKER
- none / ...

IMPORTANT
- none / ...

MINOR
- none / ...

DEFERRED
- none / ...

Known issues
- ...

Human validation
- READY / NOT READY

Git
- branch:
- commit:
- working tree:
```

Não avançar automaticamente para SM-CTX-007.

---

# 151. Handoff to Executor

## Objective

Implementar:

```text
SM-CTX-006 — Contextual Recall Core
```

## Required entry conditions

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
SM-CTX-004 DONE
SM-CTX-005 DONE
```

## Scope

```text
POST /api/v1/context/recall

resume
context

current ContextCheckpoint
current project Cognitive Memory
ACTIVE Episodes
project-wide Episodes
optional Evidence refs
bounded deterministic composition
single query embedding
degraded semantic retrieval
```

## Explicit Out of Scope

```text
explain
history
Knowledge retrieval
Dataset composition
historical checkpoint composition
RAG answer generation
LLM
Session replay
Neo4j
Recall audit persistence
```

## Expected result

Ao final:

> um Consumer consegue reconstruir o contexto atual de um projeto ou Context Stream em uma única operação estruturada e bounded, combinando onde o trabalho está, o que continua verdadeiro e o que aconteceu de relevante, sem replay de conversa e sem transformar o Sofias Memory em um agente.
