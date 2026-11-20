# Sofias Memory — Implementation: SM-CTX-007 — Explain, History and Knowledge Integration

**Document:** `implementation/15-SF-Implementation-SM-CTX-007-Explain-History-Knowledge.md`\
**Status:** APPROVED\
**Date:** 2026-11-20\
**Project:** Sofias Memory\
**Current Slice:** `SM-CTX-007 — Explain, History and Knowledge Integration`

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
- `14-SF-Implementation-SM-CTX-006-Contextual-Recall-Core.md`

---

# 1. Objective

Implementar:

```text
SM-CTX-007 — Explain, History and Knowledge Integration
```

Ao final deste Slice, `POST /api/v1/context/recall` deverá suportar integralmente os quatro intents do Contract v1:

```text
resume
context
explain
history
```

e poderá compor, quando explicitamente solicitado:

```text
current continuity
+
current cognitive truth
+
Episodes
+
historical checkpoints
+
causal Evidence
+
selected Knowledge references
```

sem:

- gerar resposta textual por LLM;
- transformar chronology em causality;
- consultar Knowledge implicitamente;
- criar Query audit;
- usar Neo4j;
- executar ações.

---

# 2. Product Value

SM-CTX-006 responde:

```text
Onde estamos?
Que contexto é relevante agora?
```

SM-CTX-007 adiciona:

```text
Como chegamos aqui?
Por que esta informação está aqui?
Que conhecimento documental relevante sustenta ou complementa este contexto?
```

A diferença é importante:

```text
resume
    = continuity

context
    = relevant present context

explain
    = explicit causal/support trace

history
    = chronological evolution
```

Esses conceitos não devem ser misturados.

---

# 3. Current Role

```text
CURRENT ROLE: Senior Software Engineer
CURRENT PHASE: Implementation
CURRENT SLICE: SM-CTX-007
```

Não alterar decisões APPROVED.

---

# 4. Required Entry State

Antes da execução:

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
SM-CTX-004 DONE
SM-CTX-005 DONE
SM-CTX-006 DONE
```

---

# 5. SM-CTX-006 MUST Provide

Todos os cenários HTTP deste handoff incluem `query` obrigatória conforme
Contract `05` §39, mesmo quando o exemplo mostra apenas o fragmento `intent`.

O Slice anterior deverá possuir:

```text
POST /api/v1/context/recall

resume
context

current checkpoint composition
current project Cognitive Memory
ACTIVE Episode retrieval
project-wide Episodes
Evidence expansion
single-query embedding
degraded semantic fallback
bounded ProjectContextBundle
```

---

# 6. Scope

Implementar neste Slice:

## Recall intents

```text
explain
history
```

## Historical composition

```text
historical ContextCheckpoints
historical Episodes
chronological ordering
```

## Explanation

```text
explicit Evidence traversal
causal dependency trace
support-aware explanation context
```

## Knowledge integration

```text
explicit dataset selection
PostgreSQL Knowledge retrieval
Knowledge references
Evidence-backed Knowledge references
bounded retrieval
degraded lexical fallback
```

## Existing intents

Permitir que:

```text
resume
context
```

também usem Knowledge quando datasets forem explicitamente fornecidos.

---

# 7. Explicit Out of Scope

MUST NOT implementar:

```text
LLM answer generation
natural-language explanation generation
RAG answer
automatic dataset selection
implicit "main" Knowledge dataset
graph Knowledge retrieval
Neo4j contextual recall
cross-project search
historical Knowledge snapshot reconstruction
historical Cognitive Memory reconstruction
semantic contradiction resolution
automatic checkpoint synthesis
contextual recall persistence/audit table
```

---

# 8. Intent Matrix

Ao final do Slice:

| Intent | Current checkpoint | Current memories | Episodes | Historical checkpoints | Evidence trace | Knowledge |
|---|---:|---:|---:|---:|---:|---:|
| `resume` | yes | yes | yes | no | optional | explicit only |
| `context` | yes | yes | yes | no | optional | explicit only |
| `explain` | yes | yes | yes | no by default | primary | explicit only |
| `history` | optional orientation | no by default | historical | yes | optional | explicit only |

Usar o Contract público exato quando houver shape mais específico.

---

# 9. `explain`

`explain` responde estruturalmente:

> de quais evidências explícitas este contexto depende?

Não responde por inferência:

> qual parece ser o motivo provável?

---

# 10. `explain` Is Evidence-Driven

Fonte de causalidade:

```text
EvidenceRef
```

Não:

```text
checkpoint revision order
superseded_by
created_at proximity
same Session
same conversation
similar text
semantic similarity
provenance alone
```

---

# 11. History Is Not Explanation

Isto:

```text
revision 1
    ↓
revision 2
    ↓
revision 3
```

prova:

```text
chronology
```

Não prova:

```text
revision 1 caused revision 2
```

Nunca transformar lineage em Evidence.

---

# 12. Explanation Root

Para `explain`, roots podem incluir recursos selecionados no bundle atual, principalmente:

```text
current ContextStatement
selected Episode
selected MemoryItem where applicable
```

Cognitive Memory não possui support graph próprio no Contract v1, portanto normalmente funciona como leaf contextual.

---

# 13. ContextStatement Explanation

Exemplo:

```text
current_state
    ↓ Evidence
Episode A
    ↓ Evidence
Chunk C
```

O bundle pode representar:

```text
current_state
    supported by Episode A
Episode A
    supported by Chunk C
```

sem gerar frase explicativa.

---

# 14. Episode Explanation

Episode pode possuir:

```text
Episode B
    ↓
Episode A
    ↓
MemoryItem M
```

A cadeia pode ser percorrida deterministicamente.

---

# 15. Evidence Trace

`explain` deve preservar relações:

```text
owner
    -> evidence source
```

Não basta retornar apenas um conjunto plano de IDs se isso destruir causalidade.

A representação pública deve seguir o Contract aprovado.

Internamente, o modelo precisa preservar pelo menos:

```text
owner_type
owner_id
support_mode
source_type
source_id
availability
```

quando aplicável.

---

# 16. Evidence Traversal

Traversal é:

- read-only;
- bounded;
- deterministic;
- PostgreSQL-only.

Não usar Neo4j.

---

# 17. Evidence Traversal Depth

Usar o limite previsto no Contract.

Se o Contract só fornecer limite agregado, aplicar hard cap interno seguro.

Não permitir dependency traversal ilimitada.

---

# 18. Evidence Traversal Breadth

Também bounded.

Um resource com grande fan-out não pode produzir resposta arbitrariamente grande.

---

# 19. Evidence Cycles

CTX-004 impede cycles.

Se runtime encontrar corrupção cíclica:

```text
fail closed or degrade safely
```

conforme o ponto da resposta.

Nunca:

- infinite loop;
- silently truncate and claim complete explanation.

---

# 20. `include_evidence`

Continuar respeitando:

```text
include_evidence
```

Não sobrescrever silenciosamente a escolha do caller.

---

# 21. `explain` With `include_evidence=true`

Retornar causal Evidence trace bounded.

---

# 22. `explain` With `include_evidence=false`

Não expandir conteúdo textual de Evidence. Preservar refs, support mode e availability dos recursos retornados conforme `05` §48; a flag false não apaga provenance nem Evidence refs. Traversal interno continua read-only e não cria public fields extras.

---

# 23. Missing Evidence

Um statement ou Episode pode ter:

```text
support = null
```

Nesse caso:

```text
no explicit causal support exists
```

Não inferir explicação.

---

# 24. Explanation Without Declared Support

`support=null` significa ausência legítima de dependency explícita, não falha de fonte. Não inventar Evidence, warning novo ou campo público de support. Retornar somente a informação disponível no shape aprovado.

---

# 25. `history`

`history` responde:

> o que aconteceu e como o Context Stream evoluiu ao longo do tempo?

Fonte principal:

```text
Episodes
+
historical ContextCheckpoints
```

---

# 26. Historical Checkpoints

`historical_checkpoints` inclui checkpoints do mesmo:

```text
(scope, context_key)
```

e nunca de outro stream.

---

# 27. Historical Lifecycle Eligibility

Elegíveis para conteúdo histórico:

```text
SUPERSEDED
INVALIDATED
```

Não retornar conteúdo semântico de:

```text
FORGOTTEN
```

---

# 28. Forgotten Historical Checkpoint

Mesmo que tombstone exista e possa ser consultado diretamente por ID:

```text
Contextual Recall history
```

não deve reconstruir ou expor semantic content esquecido.

Preferência v1:

```text
exclude FORGOTTEN checkpoints from historical_checkpoints
```

Não utilizar tombstone para preencher gaps narrativos.

---

# 29. Current Checkpoint in History

O current checkpoint não pertence à collection:

```text
historical_checkpoints
```

Pode continuar disponível no campo:

```text
current_checkpoint
```

como orientação presente.

---

# 30. Historical Checkpoint Ordering

Dentro do Context Stream:

```text
revision DESC
checkpoint_id ASC
```

ou ordem temporal equivalente definida pelo Contract.

Preferir revision como autoridade.

---

# 31. Historical Checkpoint Bound

Obrigatório.

Não retornar todo o histórico indefinidamente.

---

# 32. Historical Checkpoint Content

Cada revision preserva seu próprio snapshot:

```text
objective
current_state
approved_decisions
pending_work
open_items
blockers
next_action
```

conforme sobreviveram ao Forget propagation.

---

# 33. Historical Partial Scrub

Um checkpoint histórico pode ter perdido statements por CTX-005.

History retorna somente statements ainda existentes.

Não tentar reconstruí-los.

---

# 34. Invalidated Historical Checkpoint

Pode aparecer em `history` com:

```text
lifecycle=invalidated
```

e seu conteúdo ainda existente.

Isso informa ao Consumer:

> este estado existiu, mas posteriormente foi marcado como não confiável.

---

# 35. Episode History

Episode é naturalmente histórico.

Para intent:

```text
history
```

pode incluir:

```text
ACTIVE
INVALIDATED
```

desde que semantic content ainda exista.

---

# 36. Invalidated Episodes in History

`INVALIDATED` pode ser incluído porque:

- não foi Forgotten;
- preserva histórico;
- intent é explicitamente histórico.

MUST retornar lifecycle claramente.

Não apresentá-lo como current truth.

---

# 37. Forgotten Episodes

Nunca retornar semantic content.

Preferência:

```text
exclude from contextual history bundle
```

Direct GET por ID continua obedecendo tombstone Contract.

---

# 38. History Episode Eligibility

Mesma boundary contextual:

```text
scope = requested project

AND

(
    context_key = requested context_key
    OR context_key IS NULL
)
```

---

# 39. History Ordering

`history` deve privilegiar cronologia.

Episodes:

```text
occurred_at DESC
episode_id ASC
```

Não misturar relevance e chronology num score opaco.

---

# 40. History With Query

Quando query estiver presente, pode ser usada para:

```text
filter/select historical candidates
```

mas a apresentação final deve continuar temporalmente compreensível.

Preferência:

```text
semantic candidate selection
    ↓
chronological final ordering
```

se isso estiver coerente com o Contract.

---

# 41. Required Query and Internal Chronological Retrieval

History público exige query conforme `05` §39. Recuperação cronológica determinística sem embedding é primitive interna, reutilizável no fallback após provider failure; não constitui request público sem query.

---

# 42. Current Cognitive Memory in History

Por padrão:

```text
current_memories = []
```

para intent `history`, salvo se o Contract exigir composição diferente.

Motivo:

```text
current truth != historical timeline
```

O caller pode usar `context` ou `resume` para current truth.

---

# 43. Historical Cognitive Memory

Não implementar:

```text
superseded Cognitive Memory history
```

dentro de Contextual Recall v1.

History é:

```text
Episodes
+
Checkpoint revisions
```

Cognitive historical queries continuam responsabilidade da API cognitiva quando aplicável.

---

# 44. Knowledge Integration Principle

Knowledge só participa quando o caller informa explicitamente:

```text
datasets
```

---

# 45. No Implicit `main`

Este ponto é obrigatório.

No Knowledge Recall legado:

```text
datasets
```

pode defaultar para:

```text
["main"]
```

Contextual Recall MUST NOT copiar esse comportamento.

Aqui:

```text
datasets omitted/empty
    => no open Knowledge retrieval
```

---

# 46. Why Explicit Datasets

`project:<key>` não implica Dataset.

Nunca inferir:

```text
project:billing
    => dataset billing
```

nem:

```text
no datasets
    => main
```

---

# 47. Dataset Validation

Cada dataset explicitamente solicitado deve:

- existir;
- estar ACTIVE.

Se algum não existir/estiver indisponível:

```text
explicit request failure
```

seguindo error semantics aprovadas.

Não ignorar dataset inválido.

---

# 48. No Dataset Creation

Contextual Recall nunca cria Dataset.

---

# 49. Knowledge Retrieval Boundary

Não chamar o endpoint HTTP interno:

```http
POST /api/v1/recall
```

Não fazer localhost HTTP.

---

# 50. Do Not Call Full `RecallService`

Evitar usar diretamente o orchestration completo de:

```text
services/recall.py
```

porque ele possui comportamentos que não pertencem ao Contextual Recall:

```text
Query audit
Session admission
optional graph
RAG answer
Knowledge-specific API semantics
```

---

# 51. Internal Knowledge Retriever

Criar uma abstraction estreita, por exemplo:

```text
ContextualKnowledgeRetriever
```

ou equivalente.

Responsabilidade:

```text
explicit ACTIVE datasets
+
query
+
query embedding when available
    ↓
bounded authoritative Knowledge references
```

---

# 52. Reuse Existing Retrieval Primitives

Pode reutilizar:

```text
ChunkRepository.vector_search
ChunkRepository.lexical_search
reciprocal_rank_fusion
RecallFilters-compatible primitives
```

quando útil.

Não duplicar algoritmos já corretos.

---

# 53. No Generic Recall Framework

Não criar:

```text
UniversalRecallEngine
RetrievalPluginRegistry
ContextProviderRegistry
```

---

# 54. Knowledge Output Is Reference-Oriented

Contextual Recall não precisa devolver o corpus inteiro.

`knowledge_references[]` deve priorizar referência estruturada.

Conceitualmente:

```text
dataset_id / dataset_slug
source_id
document_id
chunk_id
relevance/score when applicable
bounded quote only if Contract explicitly provides it
```

Usar o shape exato do Contract.

---

# 55. No Automatic Full Chunk Expansion

Não retornar grandes blocos de text apenas porque um Chunk foi recuperado.

O objetivo é:

```text
contextual grounding
```

não duplicar Knowledge Recall.

---

# 56. Query + Explicit Datasets

Quando:

```text
query present
datasets non-empty
```

executar Knowledge retrieval.

---

# 57. Shared Query Embedding

Se query embedding já foi produzido por SM-CTX-006:

```text
reuse same vector
```

para:

```text
Cognitive Memory
Episodes
Knowledge vector retrieval
```

Máximo:

```text
one embedding provider call per request
```

---

# 58. Vector + Lexical Knowledge Retrieval

Quando embedding está disponível:

```text
vector retrieval
+
lexical retrieval
```

podem usar o comportamento Knowledge já estabilizado.

É aceitável reutilizar RRF **dentro da coleção Knowledge**.

Não usar RRF para fundir:

```text
Memory
Episode
Checkpoint
Knowledge
```

num ranking global.

---

# 59. Knowledge Score Is Local

Scores/ranks de Knowledge permanecem comparáveis apenas dentro de:

```text
knowledge_references
```

Não competir com Episode relevance.

---

# 60. Query Without Datasets

Nenhum open Knowledge retrieval.

Mesmo que query seja ótima.

---

# 61. Required Public Query

`datasets` não dispensa query: todo request público exige query válida. Request sem query/null é `422 INVALID_REQUEST`. Não fabricar query de current_state.

---

# 62. Evidence-Backed Knowledge

Com datasets explícitos, selected contextual resources podem já referenciar Source/Document/Chunk nesses datasets. Essas identidades podem complementar knowledge_references como direct grounding. O helper interno não requer embedding; o request HTTP ainda exige query.

---

# 63. No Public Knowledge Search Without Query

Rejeitar request HTTP sem query/null antes de Knowledge retrieval. Um helper interno sem search input não deve buscar recent chunks nem inventar query; pode retornar apenas Evidence-linked Knowledge explicitamente selecionada.

---

# 64. Knowledge Evidence Without Explicit Datasets

Evidence section pode continuar conter:

```text
source/document/chunk EvidenceRef
```

quando esses recursos sustentam contexto selecionado.

Isso não equivale a Knowledge retrieval.

Porém:

```text
knowledge_references
```

não deve ser populado por open Knowledge composition sem datasets explícitos.

---

# 65. Important Distinction

```text
evidence[]
    = explicit dependency of selected contextual resource

knowledge_references[]
    = Knowledge context explicitly requested through datasets
```

Não fundir os conceitos.

---

# 66. Dataset Filtering for Evidence-Backed Knowledge

Se caller informou:

```text
datasets=[A]
```

e Evidence aponta para Chunk de dataset B:

- EvidenceRef pode permanecer no causal Evidence trace;
- não promover esse Chunk para `knowledge_references` da seleção de A.

---

# 67. Knowledge Provider Failure

Knowledge retrieval é PostgreSQL-based.

Neo4j não participa.

---

# 68. Embedding Failure With Knowledge

Se query existe e embedding provider falha:

```text
degraded_sources identifies affected sources; warning=PARTIAL_CONTEXT
```

Knowledge pode utilizar:

```text
lexical-only fallback
```

quando datasets foram explicitamente selecionados.

---

# 69. Lexical Fallback Warning

Emitir warning indicando que semantic/vector Knowledge retrieval não ocorreu.

Não apresentar lexical-only como full semantic retrieval.

---

# 70. Cognitive/Episode Fallback

Continuar regra do CTX-006:

- deterministic current memories;
- recent Episodes;

quando query embedding falhar.

---

# 71. Single Degraded Cause

Evitar múltiplos warnings duplicados para a mesma embedding failure.

Um warning pode indicar impacto em:

```text
cognitive
episode
knowledge_vector
```

conforme Contract.

---

# 72. Knowledge PostgreSQL Failure

PostgreSQL é autoridade.

Failure de DB:

```text
request fails
```

Não retornar `knowledge_references=[]` fingindo ausência.

---

# 73. No Neo4j

Knowledge integration do Contextual Recall v1 não usa:

```text
graph
triplets
hybrid graph traversal
```

---

# 74. No Summaries

Não adicionar document-summary retrieval neste Slice salvo se o Contract explicitamente exigir.

O menor caminho é:

```text
Chunk-backed Knowledge references
```

---

# 75. No RAG

Nunca chamar:

```text
RagAnswerClient
```

---

# 76. No LLM

`explain` continua estruturado.

Não gerar:

```text
"Because X happened, Y was decided..."
```

com LLM.

---

# 77. Explain + Knowledge

Quando:

```text
intent=explain
datasets explicit
```

Knowledge pode complementar o bundle.

Mas causal explanation continua proveniente de:

```text
Evidence
```

Um Chunk semanticamente similar não vira Evidence de uma decisão apenas porque foi recuperado.

---

# 78. Semantic Relevance Is Not Causality

Regra:

```text
retrieved Knowledge
    !=
support Evidence
```

até que um caller explicitamente declare Evidence em uma mutation futura.

Recall nunca cria Evidence links.

---

# 79. History + Knowledge

Knowledge retrieval pode complementar `history` se datasets forem explicitamente enviados.

Mas Knowledge corpus atual não representa necessariamente:

```text
what the corpus looked like historically
```

Não rotular como historical snapshot.

---

# 80. No Historical Knowledge Reconstruction

Se Chunk atual é retornado durante `history`:

```text
it is current source-backed reference
```

não prova que aquele Chunk existia no momento do Episode histórico.

Não inventar temporal claim.

---

# 81. `history` Current Checkpoint Orientation

Pode retornar:

```text
current_checkpoint
```

se existir.

Isso é orientação presente.

Não o misturar na `historical_checkpoints`.

---

# 82. `explain` Historical Checkpoints

Por padrão:

```text
historical_checkpoints=[]
```

em `explain`.

Não usar lineage como causal explanation.

---

# 83. Explanation Through Episode Chain

Se current statement declara:

```text
Evidence Episode B
```

e B declara:

```text
Evidence Episode A
```

pode retornar ambas as edges.

---

# 84. Explanation Through MemoryItem

Se Evidence aponta para:

```text
MemoryItem M
```

M é normalmente leaf.

Não inferir origem causal além da provenance cognitiva.

---

# 85. Explanation Through Knowledge Evidence

Se Evidence aponta explicitamente para:

```text
Chunk C
```

a trace pode terminar em C.

Não precisa de datasets explicitamente selecionados para mostrar a Evidence identity, porque ela já faz parte da dependency explícita.

---

# 86. Evidence Availability and Persistence Lifecycle

Trace reflete availability atual. Ordinary Episode invalidation pode deixar um owner ACTIVE com support insuficiente sem Forget propagation. Aplicar eligibility de `14` §63: resume/context/explain não usam esse conteúdo como suporte confiável; history pode exibir histórico elegível com lifecycle/availability claros. Isso difere de conteúdo que deveria ter sido scrubbed após successful Forget, que é invariant failure e nunca pode ser exposto.

---

# 87. Inconsistent Support During Explain

Se resource recuperável aparece com support atualmente insuficiente:

```text
fail closed/degraded
```

conforme Contract.

Não apresentar como supported.

Não reparar durante read.

---

# 88. Explain Ordering

Evidence edges devem ter ordenação determinística.

Exemplo:

```text
owner_type
owner_id
source_type
source_id
```

---

# 89. Historical Checkpoint Repository

Adicionar primitive semelhante a:

```text
list_history(
    scope,
    context_key,
    limit,
    cursor
)
```

ou equivalente.

Elegibilidade:

```text
SUPERSEDED
INVALIDATED
```

---

# 90. Episode Historical Repository

Pode reutilizar/listar Episodes com:

```text
ACTIVE
INVALIDATED
```

e excluir FORGOTTEN.

---

# 91. History Pagination

O bundle continua bounded.

Se Contract limits suportam quantidade apenas:

```text
truncate deterministically
```

Se possui cursor/history token:

```text
obey Contract
```

Não inventar pagination pública incompatível.

---

# 92. Truncation Warning

Quando uma collection é cortada por limit:

```text
warning may indicate truncation
```

se previsto pelo Contract.

Não rotular como degraded.

---

# 93. Degraded vs Truncated

```text
degraded
    = dependency/capability unavailable

truncated
    = healthy result intentionally bounded
```

Não confundir.

---

# 94. Explain Without Current Checkpoint

É válido.

Pode explicar:

```text
selected Episodes
other contextual resources
```

se existirem.

`continuity_available=false` continua refletindo ausência de current.

---

# 95. History Without Current

Também válido.

Pode retornar:

```text
historical_checkpoints
episodes
```

sem current.

---

# 96. Empty History

Response válida:

```text
historical_checkpoints=[]
episodes=[]
```

Não 404.

---

# 97. Context Key Boundary in History

Project-wide Episodes podem aparecer.

Historical checkpoints são sempre do requested stream.

---

# 98. Cross-Stream History

Não incluir checkpoint de outro context_key.

---

# 99. Cross-Project History

Nunca.

---

# 100. Knowledge Cross-Project Semantics

Dataset é namespace Knowledge independente de project scope.

Caller pode explicitamente solicitar Dataset desde que Contract permita.

Não inferir ownership entre:

```text
project:<key>
```

e:

```text
Dataset
```

---

# 101. Security Responsibility

Dataset selection continua não sendo ACL.

Caller externo é responsável por autorização.

Sofias Memory continua single-user.

---

# 102. Contextual Recall Remains Read-Only

Mesmo em `explain/history`:

MUST NOT criar:

```text
Query
Feedback
Memory
Episode
Checkpoint
Evidence
SessionEntry
PipelineRun
graph_outbox
```

---

# 103. Do Not Persist Explain Trace

Não criar audit table.

---

# 104. Do Not Persist History Query

Não criar Query row.

---

# 105. Query Embedding Lifecycle

External embedding call:

```text
outside PostgreSQL transaction
```

Máximo uma vez.

---

# 106. Internal Chronological Fallback

History HTTP exige query. Quando embedding falha, primitive cronológica interna pode funcionar sem provider adicional; sinalizar PARTIAL_CONTEXT e degraded_sources. Não aceitar query ausente no endpoint.

---

# 107. Internal Evidence Traversal

Traversal de Evidence não requer embedding por si só. Isso é propriedade interna; explain público continua exigindo query. No fallback por embedding failure, a trace segura pode ser retornada sem nova provider call.

---

# 108. Explain With Query

Query embedding pode selecionar relevant Episodes/current memories antes de Evidence traversal.

Depois, explanation segue explicit edges.

---

# 109. Knowledge Retrieval With Query

Usar a mesma query vector.

---

# 110. Source-Specific Collections

Continuar separando:

```text
current_memories[]
episodes[]
historical_checkpoints[]
knowledge_references[]
evidence[]
```

Não produzir um único `context[]` polymorphic ranking.

---

# 111. No Global Cross-Type Ranking

Checkpoint continua prioritário por semântica.

Não por score.

---

# 112. Limits

Respeitar o shape exato do Contract.

Conceitualmente deve ser possível limitar separadamente:

```text
memories
episodes
historical checkpoints
knowledge refs
evidence
```

quando Contract permitir.

---

# 113. Server Hard Caps

Mesmo limite caller alto deve ser bounded por server hard maximum.

Não criar response unbounded.

---

# 114. Knowledge Deduplication

Vector e lexical podem recuperar o mesmo Chunk.

Retornar uma única reference.

---

# 115. Evidence/Knowledge Deduplication

Se Chunk C aparece:

```text
as explicit Evidence
```

e:

```text
as Knowledge retrieval hit
```

não apagar a distinção semântica.

Ele pode:

- aparecer na Evidence trace;
- aparecer uma vez em `knowledge_references`.

A mesma identidade física pode exercer dois papéis.

---

# 116. No Role Conflation

Não marcar:

```text
retrieved Knowledge hit
```

como:

```text
Evidence
```

apenas porque IDs coincidem.

---

# 117. Knowledge Reference Ordering

Dentro da collection:

```text
retrieval relevance/rank
```

seguido de stable UUID tie-break.

---

# 118. Evidence-Only Knowledge Reference Ordering

Sem retrieval score:

```text
source/document/chunk identity stable ordering
```

ou ordering definido pelo Contract.

Não fabricar score.

---

# 119. History Episode Score

Não fabricar similarity quando nenhum query foi usado.

---

# 120. Historical Checkpoint Score

Nunca existe relevance score.

---

# 121. Stable Public Warnings

Usar exclusivamente os warnings congelados em `05` §51: NO_CURRENT_CHECKPOINT, PARTIAL_CONTEXT, EVIDENCE_UNAVAILABLE e AMBIGUOUS_CURRENT_TRUTH. Não criar aliases para missing checkpoint, embedding failure, explanation incompleta ou truncation saudável. Campo público de degradação: degraded_sources.

---

# 122. Warning Determinism

Ordenação fixa.

---

# 123. OpenAPI

Após este Slice:

```text
intent enum
```

deve aceitar:

```text
resume
context
explain
history
```

---

# 124. Dataset Contract

`datasets` passa de:

```text
reserved but unsupported
```

para:

```text
supported explicit Knowledge selector
```

---

# 125. No New Endpoint

SM-CTX-007 não adiciona nova public route.

Tudo ocorre em:

```http
POST /api/v1/context/recall
```

---

# 126. `/api/v1/info`

Ainda MUST NOT anunciar:

```text
contextual_project_memory = "1"
```

O Contract só será ativado no SM-CTX-008.

---

# 127. Why Still Not Advertised

Mesmo com features funcionais, ainda falta:

```text
contract hardening
longitudinal validation
capability activation
documentation final
```

---

# 128. No Migration Expected

Preferência:

```text
migration = none
```

SM-CTX-007 é read behavior.

Migration só se algum index necessário estiver comprovadamente ausente.

---

# 129. Suggested Read Indexes

Se não existirem:

```text
context_checkpoints(scope, context_key, revision)

episodes(scope, context_key, lifecycle, occurred_at)

contextual_evidence_refs(source_type, source_id)

episode_evidence_refs(evidence_ref_id)

context_statement_evidence_refs(evidence_ref_id)
```

Não criar index sem query plan/uso concreto.

---

# 130. `ContextualKnowledgeRetriever`

Interface conceitual:

```python
class ContextualKnowledgeRetriever:
    async def retrieve(
        *,
        dataset_slugs: list[str],
        query: str,
        query_embedding: list[float] | None,
        limit: int,
    ) -> list[KnowledgeReference]:
        ...
```

A forma exata pode variar.

---

# 131. No Internal HTTP

O retriever usa repositories/services internos.

---

# 132. No Session Admission

Knowledge composition contextual não cria/resove Session.

---

# 133. No Knowledge Query Audit

Zero rows em:

```text
queries
```

---

# 134. Reuse RRF Locally

Se vector+lexical:

```text
reciprocal_rank_fusion
```

pode ser reutilizado.

Isso não autoriza cross-domain fusion.

---

# 135. Lexical-Only Degraded Path

Quando embedding falha:

```text
query text
    ↓
lexical Knowledge retrieval
```

pode continuar.

`degraded_sources identifies affected sources; warning=PARTIAL_CONTEXT`.

---

# 136. Internal Retriever Without Search Input

Uma primitive interna sem search input não pode retornar chunks arbitrários; no HTTP query ausente é rejeitada. Datasets explícitos não significam:

```text
return arbitrary documents
```

---

# 137. Historical Query Semantics

Se history possui query:

- use query to narrow Episodes when appropriate;
- historical checkpoints remain stream revisions, não semantic search results.

Não criar checkpoint embeddings.

---

# 138. Explain Query Semantics

Query pode selecionar which Episodes/current memories matter.

Evidence trace then starts from selected roots.

---

# 139. Explain Current Checkpoint Always Structural

Current checkpoint continua disponível independentemente da similarity query.

Não esconder continuity porque score seria baixo.

---

# 140. Current Memories in Explain

Pode utilizar current project MemoryItems selecionados semanticamente.

Eles continuam current truth, não historical Evidence.

---

# 141. MemoryItem as Evidence Leaf

Quando um ContextStatement/Episode explicitamente referencia MemoryItem:

```text
Evidence trace may include it
```

independentemente de ele também estar na `current_memories` collection.

---

# 142. Superseded MemoryItem Evidence

CTX-004 define:

```text
SUPERSEDED MemoryItem = AVAILABLE historical Evidence
```

Portanto, Evidence trace pode apontar para superseded MemoryItem.

Não promovê-lo para:

```text
current_memories
```

---

# 143. Invalidated Episode Evidence

CTX-004 define:

```text
INVALIDATED Episode = UNAVAILABLE
```

Um owner pode continuar fisicamente ACTIVE após ordinary invalidation da Evidence; aplicar read eligibility de `14` §63 sem mutação. Após successful Forget, CTX-005 exige destructive convergence.

---

# 144. History Can Show Invalidated Episode

Isso é diferente.

History explicitamente mostra o recurso histórico com lifecycle:

```text
INVALIDATED
```

Ele não está sendo usado como Evidence disponível.

---

# 145. Explain Does Not Use Invalidated Episode as Support

Mesmo que history possa mostrá-lo.

---

# 146. Forget Safety

Nenhum intent retorna forgotten semantic content.

---

# 147. Forget + History

History não é escape hatch para Forget.

Proibido reconstruir:

```text
forgotten Episode
forgotten checkpoint statement
forgotten MemoryItem
forgotten Knowledge content
```

---

# 148. Knowledge Forget Safety

Knowledge references recuperados devem obedecer authoritative Knowledge state.

Não retornar inactive/forgotten Chunk apenas porque embedding ainda existe.

---

# 149. Knowledge Evidence Identity After Forget

Evidence trace pode mostrar:

```text
availability=forgotten
```

como structural identity quando permitido.

Não retornar forgotten content.

---

# 150. Read-Time Inconsistency

Se Recall encontra resource que CTX-005 deveria ter scrubbed:

```text
do not expose semantic content
```

Fail closed ou degrade conforme Contract.

Não fazer mutation no read.

---

# 151. Unit Tests — Intent Matrix

Validar todos:

```text
resume
context
explain
history
```

---

# 152. Unit Tests — Explain Evidence

```text
Statement S -> Episode A -> Chunk C
```

Confirmar edge preservation.

---

# 153. Unit Tests — No Lineage Causality

Criar:

```text
rev1 -> rev2
```

sem Evidence.

`explain` não deve fabricar:

```text
rev1 caused rev2
```

---

# 154. Unit Tests — Explain Without Evidence

Selected resource support null.

Confirmar:

- no fabricated Evidence;
- no invented warning;
- degraded_sources remains empty.

---

# 155. Unit Tests — History Checkpoints

Seed:

```text
rev1 SUPERSEDED
rev2 INVALIDATED
rev3 ACTIVE
```

Expected:

```text
historical_checkpoints=[rev2, rev1]
current_checkpoint=rev3
```

---

# 156. Unit Tests — Forgotten History

Seed forgotten rev.

Confirmar semantic exclusion.

---

# 157. Unit Tests — Episode History

Seed:

```text
ACTIVE
INVALIDATED
FORGOTTEN
```

Expected history:

```text
ACTIVE
INVALIDATED
```

never forgotten content.

---

# 158. Unit Tests — Chronology

Episodes ordered by:

```text
occurred_at DESC
episode_id ASC
```

---

# 159. Unit Tests — No Historical Cognitive Injection

History does not suddenly load superseded MemoryItems as current context.

---

# 160. Unit Tests — Dataset Explicitness

```text
datasets=[]
    => no Knowledge retrieval

datasets=["docs"]
    => Knowledge stage runs
```

---

# 161. Unit Tests — No Main Default

Omitted datasets:

```text
Knowledge retriever calls = 0
```

mesmo se Dataset `main` existir.

---

# 162. Unit Tests — Invalid Dataset

Explicit nonexistent dataset:

```text
request fails
```

Não ignore.

---

# 163. Unit Tests — Shared Embedding

Query + memories + Episodes + Knowledge:

```text
embedding calls = 1
```

---

# 164. Unit Tests — Knowledge Lexical Fallback

Embedding provider failure + datasets + query.

Expected:

```text
lexical retrieval occurs
degraded_sources identifies affected sources; warning=PARTIAL_CONTEXT
```

---

# 165. Unit Tests — Required Public Query

Com datasets explícitos e query ausente/null, HTTP retorna 422 INVALID_REQUEST e não consulta Knowledge. Isso também vale para history/explain.

---

# 166. Unit Tests — Evidence-Backed Knowledge

Testar helper de direct grounding sem embedding: Evidence de Chunk em dataset explicitamente selecionado pode produzir knowledge_references. Exercitar via API com query válida e provider failure quando necessário, sem transformar helper em request público sem query.

---

# 167. Unit Tests — Wrong Dataset Evidence

Evidence Chunk belongs dataset B.

Caller selected A.

Expected:

```text
Evidence trace may contain identity
knowledge_references does not promote B
```

---

# 168. Unit Tests — No Knowledge Becomes Evidence

Knowledge semantic hit not linked as Evidence.

Assert no Evidence edge fabricated.

---

# 169. Unit Tests — include_evidence=false

Confirmar que não há expansão textual completa; refs e availability previstos pelo Contract permanecem nos recursos retornados. Não suprimir support/provenance nem sobrescrever request silenciosamente.

---

# 170. PostgreSQL Integration — Explain

Seed:

```text
current checkpoint.current_state
    -> Episode A
Episode A
    -> Chunk C
```

Call `explain`.

Confirm bounded causal trace.

---

# 171. PostgreSQL Integration — Explain No Evidence

Seed current statement with no support.

Confirm no inferred dependency.

---

# 172. PostgreSQL Integration — History

Create multiple checkpoint revisions and Episodes.

Confirm:

- chronological semantics;
- current/historical separation;
- invalidated visibility;
- forgotten exclusion.

---

# 173. PostgreSQL Integration — Knowledge

Seed ACTIVE Dataset with chunks.

Call context recall with explicit dataset and query.

Confirm Knowledge references.

---

# 174. PostgreSQL Integration — No Dataset

Same query, no datasets.

Confirm:

```text
knowledge_references=[]
```

---

# 175. PostgreSQL Integration — Embedding Degraded

Provider fails.

Confirm:

- current checkpoint still returned;
- deterministic memories/Episodes fallback;
- lexical Knowledge refs where possible;
- warning;
- degraded_sources identifies affected sources; warning=PARTIAL_CONTEXT.

---

# 176. PostgreSQL Integration — Forget History Safety

Create history then Forget supported derived content.

Call history.

Confirm scrubbed content absent.

---

# 177. PostgreSQL Integration — Cross-Project Isolation

No cross-project contextual root/current-memory leakage. Explicit global MemoryItem Evidence remains a reference, never automatic current_memories inclusion; Knowledge has no inferred project ownership.

---

# 178. PostgreSQL Integration — Cross-Stream History

No checkpoint leakage across Context Streams.

---

# 179. PostgreSQL Integration — Read-Only

Before/after all four intents count:

```text
queries
pipeline_runs
pipeline_steps
session_entries
memory_items
episodes
context_checkpoints
contextual_evidence_refs
graph_outbox
```

Zero writes.

---

# 180. No Neo4j Test

All intents continue working without contextual dependency on Neo4j.

---

# 181. No LLM Test

Zero completion calls for:

```text
resume
context
explain
history
```

---

# 182. Restart Validation

Persist:

```text
checkpoint revisions
Episodes
Evidence chains
Knowledge
```

Restart.

Repeat explain/history.

Results remain structurally equivalent.

---

# 183. Human Validation

## Step A — Build a realistic chain

Create:

```text
Dataset docs
    ↓
Chunk K

Episode A supported by K
    ↓
Episode B supported by A
    ↓
current checkpoint.current_state supported by B
```

Also create:

```text
rev1 SUPERSEDED
rev2 ACTIVE
```

---

## Step B — Resume

Confirm normal current continuity.

---

## Step C — Explain

Call:

```text
intent=explain
include_evidence=true
```

Confirm:

```text
current_state -> B -> A -> K
```

Não deve existir generated explanation prose.

---

## Step D — History

Call:

```text
intent=history
```

Confirm:

- rev1 historical;
- rev2 current;
- Episodes chronologically represented;
- no global memory pollution.

---

## Step E — Knowledge without datasets

Call with query but no datasets.

Confirm:

```text
knowledge_references=[]
```

---

## Step F — Knowledge with datasets

Pass:

```text
datasets=["docs"]
```

Confirm relevant references.

---

## Step G — Provider degradation

Disable embedding provider.

Repeat with query/dataset.

Confirm:

- structured bundle still usable;
- lexical Knowledge fallback;
- degraded warning.

---

## Step H — Forget

Forget K/source chain.

After successful propagation:

- affected derived semantic content absent;
- history does not reveal forgotten content;
- explain does not reconstruct it.

---

## Step I — Restart

Repeat explain/history after restart.

---

## Step J — Audit

Confirm zero:

```text
Query
PipelineRun
SessionEntry
new Evidence links
graph_outbox
```

produced by contextual recall.

---

# 184. Acceptance Criteria

## AC-007.1

`intent=explain` é suportado.

## AC-007.2

`intent=history` é suportado.

## AC-007.3

Explain usa Evidence explícita como causal source.

## AC-007.4

Explain não infere causality de checkpoint lineage.

## AC-007.5

Explain não usa LLM.

## AC-007.6

Historical checkpoints podem ser retornados.

## AC-007.7

Historical checkpoint retrieval permanece dentro do mesmo Context Stream.

## AC-007.8

SUPERSEDED checkpoints podem aparecer em history.

## AC-007.9

INVALIDATED checkpoints podem aparecer em history.

## AC-007.10

FORGOTTEN checkpoint semantic content nunca aparece em history.

## AC-007.11

ACTIVE e INVALIDATED Episodes podem participar do history conforme lifecycle explícito.

## AC-007.12

FORGOTTEN Episode semantic content nunca aparece.

## AC-007.13

Knowledge retrieval ocorre somente para datasets explicitamente selecionados.

## AC-007.14

Omitted/empty datasets não defaultam para `main`.

## AC-007.15

Explicit invalid dataset não é silenciosamente ignorado.

---

# 185. Additional Acceptance Criteria

## AC-007.16

Knowledge integration usa internal service/repository boundaries, não localhost HTTP.

## AC-007.17

Contextual Recall não cria Knowledge `Query` rows.

## AC-007.18

Contextual Recall não cria Session.

## AC-007.19

Contextual Recall não usa Neo4j.

## AC-007.20

Contextual Recall não chama RAG answer generation.

## AC-007.21

Query embedding é compartilhado entre Cognitive, Episode e Knowledge vector retrieval.

## AC-007.22

No máximo uma embedding call ocorre por request.

## AC-007.23

Embedding failure permite lexical-only Knowledge retrieval quando possível.

## AC-007.24

Embedding failure é explicitamente sinalizada como degraded.

## AC-007.25

Knowledge semantic hit não vira Evidence automaticamente.

## AC-007.26

Evidence-backed Knowledge e semantic Knowledge retrieval permanecem semanticamente distintos.

## AC-007.27

History HTTP exige query; fallback cronológico interno não exige embedding adicional.

## AC-007.28

Explain HTTP exige query; traversal interno de Evidence não exige embedding por si só.

## AC-007.29

Recall continua read-only para todos os intents.

## AC-007.30

Full Contextual Project Memory Contract ainda não é anunciado.

---

# 186. Required Test Commands

Executar:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

mais PostgreSQL integration suite contextual.

Se não houver migration:

```text
migration = none
```

deve constar no relatório.

---

# 187. Findings Classification

## BLOCKER

Exemplos:

- lineage é tratado como causal Evidence;
- forgotten content aparece em history;
- datasets omitidos consultam `main`;
- semantic Knowledge hit vira Evidence;
- explain usa LLM;
- History mistura outro Context Stream;
- Contextual Recall cria Query audit;
- embedding é chamado múltiplas vezes;
- Knowledge integration exige Neo4j.

## IMPORTANT

Afeta semântica/correctness do Slice.

## MINOR

Não impede o valor principal.

## DEFERRED

Pertence ao hardening ou evolução futura.

---

# 188. When to Stop and Escalate

Voltar para decisão humana se surgir necessidade de:

- mudar intent semantics;
- fazer lineage causal;
- incluir forgotten content em history;
- selecionar datasets automaticamente;
- adicionar default `main`;
- criar generated explanation text;
- usar LLM;
- usar Neo4j;
- adicionar historical Cognitive Memory ao bundle;
- criar historical Knowledge reconstruction;
- persistir Contextual Recall;
- alterar public ProjectContextBundle.

---

# 189. Expected File Impact

Possíveis novos arquivos:

```text
sofias_memory/services/contextual_knowledge_retriever.py
```

Alterações esperadas:

```text
sofias_memory/services/contextual_recall.py
sofias_memory/schemas/context_recall.py
sofias_memory/infrastructure/postgres/repositories/context_checkpoints.py
sofias_memory/infrastructure/postgres/repositories/episodes.py
Evidence repositories/services
Knowledge chunk repositories
tests
```

Preferência:

```text
no migration
```

---

# 190. Implementation Order

Ordem recomendada:

```text
1. verify CTX-001..006 completion
2. history repository queries
3. history composition
4. Evidence trace traversal
5. explain composition
6. explicit dataset validation
7. ContextualKnowledgeRetriever
8. Evidence-backed Knowledge refs
9. vector + lexical query Knowledge retrieval
10. shared single embedding integration
11. degraded lexical-only fallback
12. limits/truncation
13. OpenAPI intent activation
14. read-only assertions
15. Forget safety regression
16. restart validation
17. Human Validation
```

---

# 191. Slice Exit Criteria

SM-CTX-007 está pronto para closeout quando:

```text
AC-007.1..30 satisfied

all four intents usable
explain is Evidence-driven
lineage remains chronology-only
history bounded and correct
forgotten history remains absent
Knowledge requires explicit datasets
no main default
Knowledge references remain distinct from Evidence
single embedding preserved
lexical degradation proven
no LLM
no Neo4j
no Query audit
no mutation
no BLOCKER remains
```

---

# 192. Expected Implementation Report

O executor deverá retornar:

```text
SM-CTX-007 — Implementation Report

Implemented
- ...

Changed files
- ...

Database
- migration: none / ...
- schema/index changes:

Dependencies verified
- SM-CTX-001:
- SM-CTX-002:
- SM-CTX-003:
- SM-CTX-004:
- SM-CTX-005:
- SM-CTX-006:

Intent composition
- resume:
- context:
- explain:
- history:

History semantics
- checkpoint lifecycle eligibility:
- Episode lifecycle eligibility:
- ordering:
- bounds:
- forgotten exclusion:

Explanation semantics
- root selection:
- Evidence traversal:
- depth/breadth:
- lineage isolation:
- missing Evidence behavior:

Knowledge integration
- dataset validation:
- vector retrieval:
- lexical retrieval:
- RRF:
- Evidence-backed references:
- shared embedding:
- degraded fallback:

Verified
- unit:
- PostgreSQL integration:
- explain:
- history:
- lineage is not causality:
- forgotten-history exclusion:
- explicit datasets:
- no main fallback:
- invalid dataset:
- Knowledge retrieval:
- Evidence/Knowledge separation:
- single embedding:
- lexical fallback:
- required public query / internal fallback independence:
- cross-project isolation:
- cross-stream isolation:
- restart:
- read-only:
- no Neo4j:
- no LLM:
- no Query audit:
- graph isolation:
- pipeline isolation:

Acceptance Criteria
- AC-007.1 PASS/FAIL
...
- AC-007.30 PASS/FAIL

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

Não avançar automaticamente para SM-CTX-008.

---

# 193. Handoff to Executor

## Objective

Implementar:

```text
SM-CTX-007 — Explain, History and Knowledge Integration
```

## Required entry conditions

```text
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
SM-CTX-004 DONE
SM-CTX-005 DONE
SM-CTX-006 DONE
```

## Scope

```text
explain
history

historical ContextCheckpoints
historical Episodes

explicit causal Evidence trace

explicit Dataset Knowledge integration
Knowledge references
vector + lexical Knowledge retrieval
shared query embedding
lexical degraded fallback
```

## Core semantic rules

```text
lineage != causality

semantic relevance != Evidence

provenance != Evidence

project scope != Dataset

datasets omitted != main

history != forgotten-content recovery
```

## Out of Scope

```text
LLM explanation
RAG answer
automatic Dataset selection
historical Cognitive Memory composition
historical Knowledge snapshots
Neo4j Knowledge graph retrieval
Recall persistence
automatic Evidence creation
```

## Expected result

Ao final:

> o Sofias Memory consegue não apenas recuperar o contexto atual, mas também mostrar de forma estruturada como o trabalho evoluiu, quais dependências explícitas sustentam o contexto e quais referências documentais explicitamente solicitadas são relevantes — sem inventar causalidade, sem ressuscitar informação esquecida e sem assumir o papel de um agente.
