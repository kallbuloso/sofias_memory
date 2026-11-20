# Sofias Memory — Architecture: Memória Contextual Persistente por Projeto

**Document:** `06-SF-Architecture-Memoria-Contextual-Por-Projeto.md`\
**Status:** APPROVED
**Date:** 2026-11-17\
**Project:** Sofias Memory\
**Baseline:**
- `01-SF-Discovery-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `03-SF-PRD-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `04-SF-Spec-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `05-SF-Contract-Memoria-Contextual-Por-Projeto.md` — APPROVED

---

# 1. Purpose

Este documento define a arquitetura suficiente para implementar **Contextual Project Memory** no Sofias Memory sem alterar os princípios estruturais já consolidados pelo projeto.

A arquitetura deve suportar:

- `Episode`;
- `ContextCheckpoint`;
- `ContextStatement`;
- Evidence;
- Context Streams;
- Contextual Recall;
- concorrência;
- idempotência;
- Forget propagation;
- integração com Cognitive Memory;
- integração com Sessions;
- integração opcional com Knowledge Memory.

A solução deverá continuar sendo parte do **modular monolith** existente.

Não serão introduzidos novos serviços distribuídos, bancos obrigatórios, message brokers ou runtimes externos.

---

# 2. Architectural Goals

## AG-01 — Preservar o modelo atual

A feature deve integrar-se ao Sofias Memory existente sem redesenhar:

- Knowledge Memory;
- Cognitive Memory;
- Sessions;
- Skills;
- Agent Management;
- Pipeline infrastructure.

---

## AG-02 — PostgreSQL authoritative

Todo estado necessário para reconstruir Contextual Project Memory deverá existir em PostgreSQL.

---

## AG-03 — Operações contextuais críticas síncronas

Operações que determinam:

- lifecycle;
- current checkpoint;
- Evidence availability;
- Forget propagation;
- idempotency;

devem concluir de maneira consistente antes que a operação seja considerada bem-sucedida.

---

## AG-04 — Nenhuma nova infraestrutura obrigatória

O MVP contextual não deverá exigir:

- Redis;
- RabbitMQ;
- Kafka;
- novo worker externo;
- novo banco vetorial;
- novo graph database.

---

## AG-05 — Recuperação composta sem fusão de domínios

Contextual Recall deverá orquestrar os domínios existentes através de serviços internos.

Não deverá duplicar Cognitive Memory ou Knowledge Memory dentro de um novo armazenamento.

---

## AG-06 — Fail closed sobre estado

Quando o serviço não puder determinar com segurança:

- current checkpoint;
- Evidence support;
- lifecycle;

deverá falhar em vez de selecionar arbitrariamente um estado.

---

# 3. Existing Architectural Baseline

A arquitetura atual permanece:

```text
                         Clients
                            │
                            ▼
                     FastAPI / API
                            │
                            ▼
                    Domain Services
                            │
             ┌──────────────┴───────────────┐
             │                              │
             ▼                              ▼
        PostgreSQL                       Neo4j
      authoritative                  rebuildable graph
             │                         projection
             │                              ▲
             │                              │
             └──── graph_outbox ────────────┘
```

PostgreSQL continua sendo:

```text
source of truth
```

Neo4j continua sendo:

```text
rebuildable projection
```

Contextual Project Memory não altera esse princípio.

---

# 4. Target Architecture

A feature será adicionada como novos módulos internos do monólito.

```text
                           Consumers
                               │
                               ▼
                         FastAPI Routes
                               │
                ┌──────────────┴───────────────┐
                │                              │
                ▼                              ▼
        Contextual Write API            Contextual Recall API
                │                              │
                ▼                              ▼
      Contextual Domain Services      Contextual Recall Service
                │                              │
       ┌────────┼──────────┐          ┌────────┼───────────┐
       │        │          │          │        │           │
       ▼        ▼          ▼          ▼        ▼           ▼
   Episodes  Checkpoints Evidence   Cognitive Episodes  Knowledge
       │        │          │          Recall    Repo      Recall
       └────────┴──────────┘                     │
                │                                │
                └──────────────┬─────────────────┘
                               ▼
                         PostgreSQL
                         + pgvector
```

No MVP:

```text
Episodes
ContextCheckpoints
Evidence
```

não serão projetados para Neo4j.

---

# 5. Component Map

A capability será dividida conceitualmente nos seguintes módulos internos:

```text
contextual_memory/
├── episodes
├── checkpoints
├── evidence
├── recall
└── idempotency
```

Essa representação é conceitual.

A organização final de packages deverá seguir as convenções existentes do repositório.

---

# 6. API Layer

A API Layer deverá apenas:

- autenticar;
- validar payload estrutural;
- converter schemas;
- chamar Domain Services;
- converter resultados;
- mapear erros de domínio para HTTP.

Não deverá conter:

- regras de lifecycle;
- transações;
- ranking;
- Evidence evaluation;
- lógica de Forget;
- concorrência.

Rotas previstas pelo Contract:

```text
episodes.py
context_checkpoints.py
contextual_recall.py
```

---

# 7. Episode Service

Responsabilidades:

```text
create
get
list
invalidate
forget
```

O serviço deverá coordenar:

- embedding;
- Evidence validation;
- idempotency;
- lifecycle;
- persistence.

Episode Service não gerencia:

- current semantic truth;
- Cognitive Memory supersession;
- Session lifecycle;
- Agent execution.

---

# 8. ContextCheckpoint Service

Responsabilidades:

```text
create current checkpoint when no ACTIVE exists
get current
get checkpoint
list history
supersede
invalidate
forget
```

Também deverá garantir a principal invariante:

```text
max ACTIVE checkpoint = 1
per (scope, context_key)
```

A regra não poderá depender apenas do código da aplicação.

O banco deverá participar da garantia.

---

# 9. Evidence Service

Evidence será uma capability interna compartilhada por:

```text
Episode
ContextStatement
```

Responsabilidades:

- resolver referências;
- validar existência;
- verificar availability;
- impedir ciclos;
- persistir relações;
- aplicar support policy;
- localizar dependentes;
- reagir a Forget;
- atualizar availability.

Evidence Service não possuirá o conteúdo original da Evidence.

---

# 10. Contextual Recall Service

Contextual Recall será um **orchestrator de leitura**.

Ele não possuirá um novo corpus de memória.

Receberá:

```text
scope
context_key
intent
query
datasets
limits
```

e consultará somente as fontes necessárias.

Arquitetura:

```text
Contextual Recall Service
         │
         ├── Checkpoint Reader
         │
         ├── Episode Retrieval
         │
         ├── Cognitive Recall
         │
         ├── Knowledge Recall
         │
         └── Evidence Expansion
```

O serviço deverá produzir:

```text
ProjectContextBundle
```

sem gerar resposta natural final por LLM.

---

# 11. Persistence Strategy

Toda persistência contextual do MVP ficará em:

```text
PostgreSQL + pgvector
```

Não será introduzido novo datastore.

---

# 12. Proposed Relational Model

O modelo físico final será validado durante implementação, mas a arquitetura estabelece os agregados e relações necessárias.

---

## 12.1 `episodes`

Responsável pelo estado autoritativo de Episode.

Representação conceitual:

```text
episodes
├── id
├── scope
├── context_key nullable
├── kind
├── summary
├── situation nullable
├── reason nullable
├── outcome nullable
├── occurred_at
├── lifecycle
├── confidence nullable
├── support_mode nullable
├── embedding
├── provenance fields / reference
├── invalidated_at nullable
├── replacement_episode_id nullable
├── forgotten_at nullable
└── created_at
```

O conteúdo exato poderá ser normalizado entre tabela principal e tabela de provenance conforme o padrão já utilizado pelo projeto.

---

# 13. Episode Embedding

Episodes precisam participar de semantic retrieval.

Portanto, Episode ACTIVE deverá possuir embedding.

O texto canônico utilizado para embedding deverá ser derivado deterministicamente de:

```text
summary
situation
reason
outcome
```

Campos ausentes são ignorados.

Exemplo conceitual:

```text
Summary: ...
Situation: ...
Reason: ...
Outcome: ...
```

Esse texto de embedding:

- não será API pública;
- não cria uma segunda fonte de verdade;
- deverá poder ser reconstruído a partir do Episode.

---

# 14. Episode Retrieval

O MVP utilizará:

```text
pgvector exact cosine similarity
```

para Episode semantic retrieval.

Motivos:

- consistência com Native Cognitive Memory v0.7;
- baixo risco operacional;
- ausência de necessidade comprovada de ANN;
- datasets contextuais inicialmente pequenos;
- ranking determinístico mais fácil de testar.

ANN/HNSW poderá ser introduzido somente após medição que demonstre necessidade.

---

# 15. ContextCheckpoint Persistence

A persistência será dividida em:

```text
context_checkpoints
context_checkpoint_statements
```

Isso evita armazenar o checkpoint inteiro como JSON opaco.

---

## 15.1 `context_checkpoints`

Representação conceitual:

```text
context_checkpoints
├── id
├── scope
├── context_key
├── revision
├── lifecycle
├── provenance
├── superseded_at nullable
├── superseded_by nullable
├── invalidated_at nullable
├── forgotten_at nullable
└── created_at
```

---

## 15.2 `context_checkpoint_statements`

Cada unidade semanticamente independente deverá ser um registro próprio.

Tipos conceituais:

```text
objective
current_state
approved_decision
pending_work
open_item
blocker
next_action
```

Estrutura:

```text
context_checkpoint_statements
├── id
├── checkpoint_id
├── statement_type
├── ordinal
├── content
├── support_mode nullable
├── availability
└── created_at
```

Essa normalização permite:

- Evidence por statement;
- Forget parcial;
- invalidation parcial;
- ordenação determinística;
- auditoria.

---

# 16. Why Not JSON-Only Checkpoints

Um único payload JSON seria mais simples inicialmente:

```text
checkpoint.payload JSONB
```

mas criaria problemas para:

- Evidence field-level;
- Forget preciso;
- scrub parcial;
- constraints;
- queries;
- auditoria.

Portanto, o estado semântico será normalizado.

JSONB poderá existir para metadata não normativa, mas não deverá ser a representação principal do domínio.

---

# 17. Evidence Persistence

Evidence precisa referenciar recursos heterogêneos.

Será utilizado um registro central de referência contextual.

Modelo conceitual:

```text
contextual_evidence_refs
├── id
├── source_type
├── source_id
├── availability
├── unavailable_at nullable
├── forgotten_at nullable
└── created_at
```

Constraint lógica:

```text
UNIQUE(source_type, source_id)
```

---

# 18. Evidence Links

As associações serão explícitas.

```text
episode_evidence
├── episode_id
└── evidence_ref_id
```

e:

```text
checkpoint_statement_evidence
├── statement_id
└── evidence_ref_id
```

Essas tabelas deverão utilizar FKs para os recursos contextuais que pertencem ao próprio domínio.

---

# 19. Why Evidence Does Not FK Directly to Every Source

`source_type` pode representar recursos de domínios distintos:

```text
session_entry
memory_item
episode
source
document
chunk
```

Uma única FK relacional não pode apontar para múltiplas tabelas.

Portanto:

```text
(source_type, source_id)
```

será validado pelo Domain Service no momento da criação.

Depois disso, `contextual_evidence_refs` preservará a identidade da referência mesmo se a fonte posteriormente for esquecida ou removida segundo seu próprio lifecycle.

---

# 20. Evidence Resolution

Evidence Service deverá utilizar adapters internos por `source_type`.

Conceitualmente:

```text
EvidenceResolver
    ├── SessionEntryResolver
    ├── MemoryItemResolver
    ├── EpisodeResolver
    ├── SourceResolver
    ├── DocumentResolver
    └── ChunkResolver
```

Esses adapters são internos.

Não constituem plugins ou interfaces públicas.

---

# 21. Evidence Cycle Detection

Como `episode` pode ser Evidence de outro Episode, ciclos são possíveis.

Antes da criação do link:

```text
A -> B
```

o serviço deverá verificar se B já depende transitivamente de A.

Se sim:

```text
INVALID_EVIDENCE_CYCLE
```

A verificação poderá utilizar PostgreSQL recursive CTE.

Não há necessidade de Neo4j para essa operação.

---

# 22. Support Policy Persistence

`support_mode` será armazenado no owner semântico:

Para Episode:

```text
episodes.support_mode
```

Para statement:

```text
context_checkpoint_statements.support_mode
```

Valores:

```text
all_required
any_sufficient
```

Quando não existir Evidence:

```text
support_mode = NULL
```

---

# 23. Evidence Support Evaluation

A avaliação deverá ser puramente determinística.

### `all_required`

```text
supported =
  every evidence availability == AVAILABLE
```

### `any_sufficient`

```text
supported =
  at least one evidence availability == AVAILABLE
```

Não haverá:

- LLM evaluator;
- confidence model;
- semantic inference.

---

# 24. Checkpoint Current-State Constraint

A principal invariante deverá possuir proteção no banco.

Conceitualmente:

```text
UNIQUE(scope, context_key)
WHERE lifecycle = 'active'
```

Isso impede dois checkpoints ACTIVE mesmo sob concorrência entre processos.

A aplicação não dependerá apenas de:

```text
SELECT then INSERT
```

---

# 25. Revision Constraint

Dentro de:

```text
(scope, context_key)
```

revision será monotônica.

Constraint:

```text
UNIQUE(scope, context_key, revision)
```

A nova revisão é calculada na transação que serializa a escrita do stream. Supersede bloqueia o current; Create sem ACTIVE usa proteção database-authoritative conforme handoff `10`, inclusive sem row histórica para bloquear. Revision é 1 para stream novo e MAX(existing revision)+1 para histórico sem ACTIVE. Checkpoint tombstones preservam scope/context_key/revision e participam dessa sequência; Forget não permite reuse.

---

# 26. Checkpoint Supersession Transaction

Operação:

```text
A -> B
```

será executada em uma única transação PostgreSQL.

Fluxo:

```text
BEGIN

resolve idempotency

SELECT current checkpoint
FOR UPDATE

verify:
  current.id == expected checkpoint

calculate next revision

insert new checkpoint
insert statements
insert Evidence links

mark old checkpoint SUPERSEDED
set superseded_by

COMMIT
```

A ordem física poderá ser adaptada para satisfazer constraints, mas a atomicidade pública permanece essa.

---

# 27. Concurrency Model

Não será criado lock distribuído externo.

PostgreSQL fornecerá:

- row locks;
- transactions;
- uniqueness constraints.

Isso é suficiente para o baseline single-user e para múltiplos callers concorrentes.

---

# 28. Idempotency Persistence

Contextual Project Memory terá armazenamento de idempotência próprio.

Modelo conceitual:

```text
contextual_memory_idempotency
├── operation_digest
├── request_digest
├── operation_type
├── resource_type
├── resource_id nullable
├── result_descriptor
└── created_at
```

A estratégia deverá seguir os princípios já adotados por Native Cognitive Memory:

- chave não armazenada em texto puro;
- digest não reversível;
- same key + same request = replay;
- same key + different request = conflict.

---

# 29. Why Separate Contextual Idempotency State

O armazenamento existente de Cognitive Memory pertence ao contrato cognitivo.

Contextual Project Memory deverá possuir namespace/estado próprio para:

- não alterar semântica v0.7;
- permitir evolução independente;
- evitar colisões entre contracts;
- facilitar migrations e auditoria.

Implementação utilitária poderá ser compartilhada internamente.

Estado autoritativo não deverá ser misturado silenciosamente.

---

# 30. Write Path — Episode

```text
HTTP request
     │
     ▼
Episode Route
     │
     ▼
Episode Service
     │
     ├── validate scope
     ├── validate provenance
     ├── resolve Evidence
     ├── detect cycles
     ├── generate embedding
     │
     ▼
PostgreSQL transaction
     │
     ├── resolve idempotency
     ├── create/reuse Evidence refs
     ├── insert Episode
     ├── insert Evidence links
     └── persist idempotency result
```

Embedding provider call SHOULD ocorrer antes da transação curta.

Evidence MUST ser revalidada dentro da authoritative write path antes do commit quando seu estado puder ter mudado.

---

# 31. Write Path — Create Current Checkpoint

```text
request
   │
   ▼
Checkpoint Service
   │
   ├── validate scope/context_key
   ├── validate statements
   ├── resolve Evidence
   │
   ▼
transaction
   │
   ├── resolve idempotency
   ├── verify no ACTIVE checkpoint
   ├── allocate 1 for new stream, otherwise MAX(existing revision)+1
   ├── insert new ACTIVE checkpoint
   ├── insert statements
   ├── insert Evidence links
   └── commit
```

Nenhum embedding é necessário para current checkpoint no MVP.

Este fluxo inclui recovery Create depois de Invalidate/Forget, sem reativação histórica ou supersession lineage implícita, conforme handoff `10` APPROVED.

---

# 32. Why Checkpoints Do Not Need Embeddings Initially

O current checkpoint é acessado diretamente por:

```text
(scope, context_key)
```

e possui função de continuidade.

Historical retrieval pode inicialmente utilizar:

- revision;
- chronology;
- context_key;
- bounded recent history.

Episodes são o principal mecanismo para recuperação histórica semântica.

Criar embeddings de checkpoints agora adicionaria:

- custo;
- armazenamento;
- mais uma estratégia de ranking;

sem necessidade demonstrada.

---

# 33. Read Path — Resume

```text
Contextual Recall
intent = resume
        │
        ├── current checkpoint
        ├── Cognitive Recall
        └── Episode retrieval
```

Current checkpoint será lookup direto.

Cognitive Memory será consultada através do serviço interno existente.

Episode retrieval utilizará query embedding + pgvector.

Knowledge Memory não será chamada automaticamente sem datasets explícitos.

---

# 34. Read Path — Explain

```text
query
  │
  ▼
query embedding
  │
  ├── Cognitive Recall
  ├── Episode semantic retrieval
  └── optional Knowledge Recall
```

Evidence poderá ser expandida depois da seleção inicial.

Isso evita expandir fontes que não entrarão no resultado final.

---

# 35. Read Path — History

```text
query
  │
  ├── Episode semantic retrieval
  ├── bounded checkpoint history
  └── current orientation when requested by the approved intent plan
```

O resultado preservará classificação explícita.

Histórico cognitivo via as_of continua pertencendo à API cognitiva; handoff `15` não o inclui no Contextual Recall v1. History contextual usa Episodes e checkpoint revisions, sem reconstruir snapshots históricos de Knowledge.

---

# 36. Read Path — Context

```text
query
  │
  ├── current checkpoint
  ├── Cognitive Recall
  ├── Episode retrieval
  └── optional Knowledge Recall
```

Cada fonte retorna candidatos.

O Contextual Recall Service aplica composição e limits.

---

# 37. Retrieval Composition

O MVP não utilizará um LLM router.

A escolha de fontes será determinada pelo:

```text
intent
```

O ranking final será composto deterministicamente.

Estratégia inicial:

```text
source-specific ranking
        ↓
intent weighting
        ↓
bounded merge
```

---

# 38. Source-Specific Ranking

## Current Checkpoint

Não possui ranking.

Existe zero ou um.

---

## Cognitive Memory

Reutiliza o ranking do serviço de Cognitive Recall.

---

## Episodes

Utiliza:

```text
cosine relevance DESC
occurred_at DESC
episode_id ASC
```

como tie-break determinístico.

---

## Historical Checkpoints

Utiliza:

```text
revision DESC
checkpoint_id ASC
```

dentro do Context Stream.

---

## Knowledge

Reutiliza o ranking do serviço existente solicitado pelo Contextual Recall.

---

# 39. Cross-Source Composition

Os scores de diferentes mecanismos não devem ser tratados ingenuamente como se fossem matematicamente equivalentes.

Portanto, o MVP não fará:

```text
sort all items by raw score
```

A composição utilizará quotas/pesos por intent.

Exemplo conceitual para `resume`:

```text
current checkpoint     mandatory when available
current memories       bounded set
episodes               bounded set
knowledge              optional
```

Os valores exatos serão configuração/implementação e deverão ser validados empiricamente.

---

# 40. Future Rank Fusion

Se benchmarks demonstrarem necessidade, uma evolução poderá introduzir:

```text
Reciprocal Rank Fusion
```

ou outro mecanismo de rank fusion entre domínios.

Isso não é requisito arquitetural do MVP; não impede reutilizar RRF existente somente dentro de Knowledge retrieval, conforme handoff `15`.

---

# 41. Knowledge Recall Integration

Contextual Recall não chamará a própria API HTTP internamente.

Ele reutilizará Domain Services / retrieval components existentes.

Fluxo correto:

```text
ContextualRecallService
        │
        └── KnowledgeRecallService
```

não:

```text
ContextualRecallService
        │
        └── HTTP localhost /api/v1/recall
```

---

# 42. Cognitive Recall Integration

A mesma regra se aplica à Cognitive Memory.

```text
ContextualRecallService
        │
        └── CognitiveMemoryRecallService
```

O serviço existente continua dono de:

- temporal filtering;
- scope filtering;
- cognitive lifecycle;
- relevance.

Contextual Recall não duplica essas regras.

---

# 43. Session Integration

Sessions continuam exclusivamente PostgreSQL.

Contextual Project Memory poderá:

- validar SessionEntry como Evidence;
- expandir SessionEntry quando solicitado;
- preservar sua referência.

Não alterará:

- Session lifecycle;
- archive;
- SessionEntry append-only semantics.

---

# 44. Forget Architecture

Forget propagation precisa preservar uma garantia forte:

> depois que Forget de uma Evidence for considerado concluído, seus derivados não poderão continuar expondo conteúdo que dependa exclusivamente dela.

Para isso, a propagação autoritativa será **síncrona no PostgreSQL**.

---

# 45. No Eventual Forget Window

Não será utilizado o modelo:

```text
forget source
   ↓
commit
   ↓
background job later
   ↓
invalidate derived context
```

porque existiria uma janela na qual:

```text
source = forgotten
derived content = still recallable
```

Isso violaria o Contract.

---

# 46. Contextual Evidence Invalidation Hook

Domínios internos que executam Forget deverão integrar-se a um serviço interno:

```text
ContextualEvidenceInvalidator
```

Conceitualmente:

```text
authoritative source Forget
          │
          ▼
mark matching contextual Evidence unavailable/forgotten
          │
          ▼
evaluate dependents
          │
          ▼
scrub/invalidate contextual content when required
```

Tudo que precisa ser atomicamente coerente deverá participar da mesma transação PostgreSQL da transição autoritativa correspondente.

---

# 47. Knowledge Forget Integration

Knowledge Forget já pode envolver PipelineRun e múltiplas etapas.

A integração contextual deverá ocorrer na etapa em que o estado autoritativo PostgreSQL da Evidence deixa efetivamente de estar disponível.

Essa mesma unidade transacional deverá:

- alterar/remover o conhecimento autoritativo;
- atualizar `contextual_evidence_refs`;
- aplicar derived invalidation necessária;
- produzir graph outbox quando aplicável ao Knowledge domain.

Contextual invalidation não depende do Neo4j.

---

# 48. Cognitive Forget Integration

Cognitive Forget é síncrono.

Na mesma transação autoritativa em que o MemoryItem é scrubbed:

```text
MemoryItem Forget
      +
Evidence availability update
      +
contextual derived invalidation
```

devem convergir para estado consistente.

---

# 49. Direct Episode Forget

Episode pode servir como Evidence de outro Episode.

Portanto:

```text
Forget Episode A
      ↓
mark Episode A forgotten
      ↓
mark EvidenceRef(A) forgotten
      ↓
evaluate dependents
```

A propagação é transitiva.

---

# 50. Transitive Propagation

Como ciclos são proibidos, as dependências formam um DAG contextual.

Forget/invalidation pode percorrer:

```text
A
└── B
    └── C
        └── D
```

quando cada derivado depender do anterior.

A propagação deverá continuar até atingir estado estável.

---

# 51. Propagation Algorithm

O MVP poderá utilizar:

- recursive CTE;
- fila em memória dentro da operação;
- iteração controlada no Domain Service.

Não será necessário message broker.

A implementação deverá possuir proteção contra:

- loops inesperados;
- profundidade excessiva;
- inconsistência de dados.

Ciclo detectado em dados persistidos deve ser tratado como corrupção/invariante quebrada e fail closed.

---

# 52. Partial Checkpoint Scrub

Quando Evidence de um ContextStatement for perdida:

```text
statement support evaluation
```

determina seu estado.

Se unsupported:

```text
statement content -> scrubbed/unavailable
```

O restante do checkpoint pode permanecer.

---

# 53. Current-State Minimum

Um ACTIVE checkpoint necessita de:

```text
current_state
```

utilizável.

Se o `current_state` se tornar unsupported:

```text
checkpoint ACTIVE -> INVALIDATED
```

Nenhum checkpoint histórico será reativado.

---

# 54. Why No Automatic Rollback

Reativar checkpoint anterior produziria potencialmente estado obsoleto como se fosse atual.

Exemplo:

```text
A = arquitetura inicial
B = evolução posterior

B invalidado
```

não significa:

```text
A voltou a ser verdade
```

Portanto:

```text
current becomes absent
```

é semanticamente mais seguro.

---

# 55. Episode Invalidation vs Forget

Os dois comportamentos permanecem separados.

### Invalidate

```text
conteúdo preservado
normal recall disabled
historical/admin inspection possible
```

### Forget

```text
conteúdo scrubbed
recovery intentionally impossible
```

---

# 56. Checkpoint Invalidation vs Forget

Mesma distinção:

### Invalidated

Registro permanece para inspeção e auditabilidade.

### Forgotten

Conteúdo recuperável é destruído.

---

# 57. Neo4j Decision

No MVP:

```text
Episode              -> PostgreSQL only
ContextCheckpoint    -> PostgreSQL only
ContextStatement     -> PostgreSQL only
Evidence             -> PostgreSQL only
```

Não haverá:

- graph_outbox events;
- Episode nodes;
- Checkpoint nodes;
- Evidence relationships em Neo4j.

---

# 58. Rationale for No Contextual Graph Projection

A capability inicial já pode oferecer:

- continuidade;
- histórico;
- causal recall via Episodes;
- Evidence traversal;
- project isolation;
- semantic retrieval;

usando PostgreSQL + pgvector.

Introduzir Neo4j agora aumentaria:

- schema;
- reconciliation;
- outbox events;
- rebuild requirements;
- Forget complexity;
- testes;
- operational surface.

Sem benefício ainda demonstrado.

---

# 59. Future Graph Evolution

Uma futura feature poderá projetar:

```text
Episode -> Entity
Episode -> Episode
Episode -> Decision
Cause -> Effect
```

somente se benchmarks demonstrarem valor significativo.

Se implementada:

```text
PostgreSQL remains authoritative
Neo4j remains rebuildable
```

conforme ADR-0002.

---

# 60. No New PipelineRun for Contextual Writes

Operações:

```text
Episode create
Episode invalidate
Episode forget
Checkpoint create
Checkpoint supersede
Checkpoint invalidate
Checkpoint forget
```

serão síncronas.

Não criarão `PipelineRun`.

Motivos:

- mutations pequenas;
- caller necessita conhecer o estado imediatamente;
- lifecycle e concorrência precisam concluir atomicamente;
- não existe processamento pesado obrigatório.

---

# 61. Contextual Recall Is Synchronous

Contextual Recall será uma leitura síncrona.

Não criará PipelineRun.

Ele poderá reutilizar serviços internos que façam:

- embedding externo;
- PostgreSQL queries;
- Knowledge retrieval.

---

# 62. External Provider Dependency

Episode create necessita de embedding.

Contextual Recall normalmente necessita embedding da query para:

- Episodes;
- Cognitive Memory;
- Knowledge Memory vector modes.

O sistema deverá reutilizar o provider de embeddings já configurado.

Não será introduzido provider específico para Contextual Memory.

---

# 63. Provider Failure

Quando Episode Create precisar de embedding e o provider estiver indisponível:

```text
write fails
no partial Episode persists
```

Quando Contextual Recall tiver fontes independentes:

```text
provider-dependent retrieval may degrade
```

se for possível produzir contexto útil sem violar Contract.

Exemplo:

```text
resume
```

pode ainda retornar current checkpoint mesmo que semantic retrieval esteja degradado.

---

# 64. Transaction Boundaries

Transações devem permanecer curtas.

Chamadas externas:

```text
embedding provider
LLM provider
external API
```

MUST NOT ocorrer dentro de transação PostgreSQL aberta, salvo justificativa futura explícita.

---

# 65. Episode Create Transaction Boundary

```text
validate request
resolve provider input
generate embedding
        ↓
BEGIN
  resolve idempotency
  revalidate Evidence authoritative state
  create Evidence refs
  insert Episode
  links
  idempotency result
COMMIT
```

---

# 66. Checkpoint Write Transaction Boundary

Não existe provider call obrigatório.

Portanto:

```text
validate request
        ↓
BEGIN
  resolve idempotency
  validate/revalidate Evidence
  lock current state
  write checkpoint
  write statements
  write evidence links
  update lineage
  store idempotency result
COMMIT
```

---

# 67. Contextual Recall Execution

Conceitualmente:

```text
validate request
      ↓
derive source plan from intent
      ↓
create query embedding when required
      ↓
run independent retrievals
      ↓
classify failures
      ↓
merge bounded results
      ↓
optional Evidence expansion
      ↓
return ProjectContextBundle
```

---

# 68. Parallel Retrieval

Quando as fontes forem independentes, poderão ser consultadas concorrentemente:

```text
Cognitive Recall
Episode Recall
Knowledge Recall
```

Current Checkpoint pode ser buscado em paralelo.

Isso é otimização interna.

O resultado deve permanecer determinístico segundo os critérios de composição.

---

# 69. Degraded Read Model

Cada retrieval source terá estado interno:

```text
success
empty
degraded
failed_required
```

O Contextual Recall Service decidirá:

- retornar resultado parcial;
- retornar warning;
- falhar.

Nenhum missing source será substituído por inferência inventada.

---

# 70. API Schemas

Schemas públicos deverão ser separados dos models ORM.

Estrutura conceitual:

```text
schemas/
├── episodes.py
├── context_checkpoints.py
└── contextual_recall.py
```

Domínio e persistence não deverão depender dos schemas HTTP.

---

# 71. Repository Layer

Persistência contextual utilizará repositories específicos.

Conceitualmente:

```text
EpisodeRepository
ContextCheckpointRepository
ContextStatementRepository
EvidenceRepository
ContextualIdempotencyRepository
```

Repositories não conterão regras de produto.

---

# 72. Service Layer

Regras ficam em services.

Conceitualmente:

```text
EpisodeService
ContextCheckpointService
ContextualRecallService
ContextualEvidenceService
ContextualEvidenceInvalidator
```

A implementação deve evitar uma camada de abstração genérica do tipo:

```text
GenericMemoryRepository
GenericLifecycleEngine
GenericContextResource
```

no MVP.

Os domínios possuem semânticas diferentes e devem permanecer explícitos.

---

# 73. Domain Models

Enums internos deverão representar:

```text
EpisodeLifecycle
EpisodeKind
CheckpointLifecycle
StatementType
EvidenceSourceType
EvidenceAvailability
EvidenceSupportMode
ContextRecallIntent
```

Esses conceitos já são normativos pelo Contract.

---

# 74. Application Package Direction

A implementação deverá respeitar a estrutura atual do projeto.

Uma possível organização é:

```text
sofias_memory/
├── api/
│   └── routes/
│       ├── episodes.py
│       ├── context_checkpoints.py
│       └── contextual_recall.py
│
├── schemas/
│   ├── episodes.py
│   ├── context_checkpoints.py
│   └── contextual_recall.py
│
├── domain/
│   └── contextual_memory/
│       ├── episodes.py
│       ├── checkpoints.py
│       └── evidence.py
│
├── services/
│   ├── episodes.py
│   ├── context_checkpoints.py
│   ├── contextual_recall.py
│   └── contextual_evidence.py
│
└── infrastructure/
    └── postgres/
        └── repositories/
```

Os nomes finais podem seguir convenções locais existentes.

A separação de responsabilidades é mais importante que essa árvore literal.

---

# 75. Migration Strategy

A feature exigirá migrations PostgreSQL aditivas.

Migrations deverão:

- criar novas tabelas;
- criar constraints;
- criar indexes;
- não alterar significado das tabelas existentes;
- preservar upgrade automático conforme ADR-0015.

Não haverá migration Neo4j.

---

# 76. Existing Data Migration

Não haverá backfill automático de:

```text
SessionEntry -> Episode
Session -> Checkpoint
MemoryItem -> Episode
metadata -> ContextCheckpoint
```

Contextual Project Memory começa a existir a partir da nova capability.

Dados históricos permanecem em seus domínios atuais.

---

# 77. Why No Historical Backfill

Inferir Episodes e Checkpoints de dados antigos criaria:

- falsa proveniência;
- decisões possivelmente erradas;
- continuidade inventada;
- inconsistência com supervised-first.

Uma ferramenta de importação futura poderá ser criada explicitamente, mas não fará parte da migration.

---

# 78. Index Strategy

Índices mínimos conceituais:

### Episodes

```text
(scope, lifecycle)
(scope, context_key, lifecycle)
(kind)
(occurred_at)
pgvector embedding index only when justified
```

Para o MVP, exact cosine não exige ANN index.

---

### Checkpoints

```text
(scope, context_key, lifecycle)
(scope, context_key, revision)
```

e unique partial constraint para ACTIVE.

---

### Statements

```text
(checkpoint_id, statement_type, ordinal)
```

---

### Evidence

```text
UNIQUE(source_type, source_id)
```

Links:

```text
episode_id
evidence_ref_id
statement_id
```

---

# 79. Performance Posture

Não serão estabelecidos números arbitrários antes de benchmark.

A arquitetura prioriza:

- bounded queries;
- indexed scope filtering;
- exact vector retrieval inicialmente;
- no full Session scans;
- no full Project Memory scans;
- Evidence expansion somente após seleção.

---

# 80. Benchmark Requirements

Antes de introduzir otimizações estruturais, deverão ser medidos:

- Episode recall latency;
- Contextual Recall latency por intent;
- checkpoint lookup latency;
- Evidence propagation cost;
- Project Memory size;
- number of Episodes;
- number of Evidence links;
- query embedding cost.

ANN ou graph projection somente deverão ser adicionados se esses dados justificarem.

---

# 81. Observability

A feature deverá reutilizar logging/metrics conventions existentes.

Structured context SHOULD incluir:

```text
operation
scope
context_key
episode_id
checkpoint_id
revision
intent
result_count
degraded_sources
```

Conteúdo textual de memória não deverá ser logado por padrão.

---

# 82. Health and Readiness

Nenhuma nova dependência de infraestrutura será adicionada.

Portanto, health/readiness continua verificando as dependências existentes.

Contextual Memory não cria novo health check externo.

---

# 83. Backup and Restore

Como Contextual Project Memory é autoritativa em PostgreSQL:

```text
PostgreSQL backup
```

deverá preservar completamente:

- Episodes;
- Checkpoints;
- Statements;
- Evidence;
- lineage;
- idempotency.

Nenhum backup adicional de Neo4j será necessário para a feature.

---

# 84. Disaster Recovery

Após restore PostgreSQL:

```text
current checkpoint
Episode lifecycle
Evidence availability
Forget state
idempotency
```

devem ser reconstruídos automaticamente do próprio banco.

Não poderá existir estado crítico somente:

- em memória;
- em cache;
- em Neo4j.

---

# 85. Security Architecture

A feature reutiliza:

```text
X-API-Key
```

do baseline atual.

Não haverá:

- API key por project;
- user auth;
- per-scope permissions;
- ACL.

Esses temas exigiriam feature/ADR separados.

---

# 86. Context Injection Safety

Contextual Recall retorna dados.

Nenhum resultado poderá ser colocado pelo Sofias Memory em uma posição privilegiada equivalente a:

```text
system instruction
policy
permission
```

Essa responsabilidade pertence ao Consumer.

---

# 87. External References and Privacy

External provenance refs deverão armazenar somente o necessário para rastreabilidade.

Provider-specific identifiers sem função de memória não deverão atravessar o boundary.

A regra existente que exclui Provider Session IDs permanece.

---

# 88. Testing Architecture

A implementação deverá utilizar três níveis principais.

---

## 88.1 Unit Tests

Para:

- support policy;
- lifecycle;
- context key validation;
- Evidence cycle detection;
- recall source planning;
- result classification;
- propagation decisions.

---

## 88.2 PostgreSQL Integration Tests

Obrigatórios para:

- unique ACTIVE constraint;
- checkpoint race;
- idempotency race;
- revision monotonicity;
- Episode persistence;
- Evidence persistence;
- Forget propagation;
- transitive invalidation;
- restart durability.

PostgreSQL deverá ser real nos testes de integração críticos.

---

## 88.3 Cross-Feature Integration Tests

Para:

```text
Contextual Memory ↔ Cognitive Memory
Contextual Memory ↔ Sessions
Contextual Memory ↔ Knowledge Forget
Contextual Recall ↔ Knowledge Recall
```

---

# 89. No Neo4j Test Dependency for MVP Contextual Domain

Testes de Episodes/Checkpoints não deverão exigir Neo4j.

Um teste cross-feature poderá provar explicitamente que:

```text
contextual writes produce zero graph_outbox events
```

e que indisponibilidade do Neo4j não muda a semântica desses writes.

---

# 90. Longitudinal Validation

Além dos testes convencionais, deverá existir um cenário longitudinal que prove:

```text
Session A
   ↓
persist contextual memory
   ↓
restart
   ↓
Session B
   ↓
resume correctly
```

Essa validação é obrigatória para comprovar o valor principal da feature.

---

# 91. Failure Recovery

## Write failure before commit

Nenhum estado contextual deve aparecer.

## Write failure after commit / response lost

Idempotency replay recupera o resultado.

## Stale checkpoint

Current state permanece inalterado.

## Embedding failure before Episode write

Nenhum Episode parcial.

## Contextual Recall source degradation

Resultado parcial somente quando seguro e explicitamente sinalizado.

---

# 92. Architecture Boundaries

| Concern | Component |
|---|---|
| HTTP validation | API/Schemas |
| Episode semantics | EpisodeService |
| Checkpoint semantics | ContextCheckpointService |
| Evidence relationships | ContextualEvidenceService |
| Forget propagation | ContextualEvidenceInvalidator |
| Context composition | ContextualRecallService |
| Cognitive truth | Existing Cognitive Memory |
| Session history | Existing Sessions |
| Knowledge corpus | Existing Knowledge Memory |
| Persistence | PostgreSQL repositories |
| Graph projection | Existing Knowledge graph only |
| Agent decisions | External Consumer |

---

# 93. Rejected Architectural Alternatives

## 93.1 Store everything as MemoryItem

Rejected.

Motivo:

- Episode possui semântica histórica distinta;
- Checkpoint possui revision/current semantics próprias.

---

## 93.2 Store checkpoint as Session metadata

Rejected.

Motivo:

- Session não representa durable project context;
- project work atravessa Sessions;
- concorrência e revision lineage ficariam frágeis.

---

## 93.3 Store entire checkpoint as JSONB

Rejected como representação autoritativa principal.

Motivo:

- Forget field-level;
- Evidence relations;
- constraints;
- auditability.

---

## 93.4 Project-specific Dataset

Rejected.

Dataset representa Knowledge corpus.

Não representa Project Context.

---

## 93.5 Neo4j as Episode store

Rejected.

Violaria source-of-truth architecture e adicionaria complexidade sem necessidade.

---

## 93.6 Background eventual Forget propagation

Rejected.

Criaria janela de resurrection de conteúdo esquecido.

---

## 93.7 New message broker

Rejected.

PostgreSQL e serviços internos são suficientes.

---

## 93.8 Generic Memory Aggregate

Rejected.

A abstração esconderia diferenças importantes entre:

- semantic truth;
- experience;
- temporal history;
- continuity state.

---

## 93.9 Automatic distillation in MVP

Rejected.

Contraria supervised-first e introduz risco de memory pollution antes da validação do modelo.

---

# 94. Decisions Requiring ADR

A arquitetura introduz decisões duráveis que deverão ser registradas formalmente em ADRs antes da implementação.

---

## ADR-A — Contextual Memory Domain and PostgreSQL Authority

Deverá registrar:

- Episode e ContextCheckpoint como domínios first-class;
- PostgreSQL como autoridade;
- ausência de Neo4j projection no MVP;
- ausência de PipelineRun para contextual mutations;
- ausência de backfill automático.

---

## ADR-B — ContextCheckpoint Revision and Concurrency Model

Deverá registrar:

- Context Stream `(scope, context_key)`;
- single ACTIVE checkpoint;
- full replacement;
- atomic supersession;
- optimistic stale detection;
- PostgreSQL uniqueness/locking;
- no automatic rollback.

---

## ADR-C — Provenance-Aware Evidence and Synchronous Forget Propagation

Deverá registrar:

- Evidence refs;
- `all_required` / `any_sufficient`;
- field-level checkpoint Evidence;
- cycle prohibition;
- synchronous propagation;
- transitive invalidation;
- no LLM decision;
- no eventual inconsistency window.

---

# 95. Deferred Architecture

As seguintes decisões permanecem intencionalmente abertas:

- MemorySpace;
- Episode graph projection;
- ANN/HNSW;
- RRF;
- semantic checkpoint retrieval;
- automatic distillation;
- context compaction;
- automatic contradiction detection;
- cross-project recall;
- global + project automatic composition;
- multi-user isolation;
- distributed deployment.

Elas não são necessárias para implementar o Contract v1.

---

# 96. Target Data Flow Summary

```text
Consumer
   │
   ├── write Episode
   │       │
   │       ├── embedding
   │       ├── Evidence validation
   │       └── PostgreSQL commit
   │
   ├── write Checkpoint
   │       │
   │       ├── statements
   │       ├── Evidence validation
   │       └── atomic PostgreSQL revision
   │
   └── Contextual Recall
           │
           ├── current checkpoint
           ├── Cognitive Memory
           ├── Episode semantic recall
           ├── optional Knowledge Memory
           └── Evidence expansion
                    │
                    ▼
              ProjectContextBundle
```

---

# 97. Forget Data Flow Summary

```text
Authoritative resource
        │
        ▼
      Forget
        │
        ▼
PostgreSQL authoritative mutation
        │
        ├── mark contextual Evidence unavailable
        │
        ├── evaluate direct dependents
        │
        ├── scrub / invalidate unsupported content
        │
        ├── propagate transitively
        │
        └── commit consistent state
```

Nenhum conteúdo derivado unsupported deverá permanecer recuperável após sucesso da operação.

---

# 98. Architecture Summary

A solução será implementada como extensão do modular monolith existente:

```text
Sofias Memory
│
├── Knowledge Memory
├── Cognitive Memory
├── Sessions
│
└── Contextual Project Memory
    │
    ├── Episodes
    ├── Context Streams
    │    └── ContextCheckpoints
    │         └── ContextStatements
    │
    ├── Evidence
    └── Contextual Recall
```

Persistência:

```text
PostgreSQL + pgvector
```

Neo4j:

```text
unchanged
knowledge projection only
```

Operações contextuais:

```text
synchronous
transactional
idempotent
```

Recuperação:

```text
intent-driven
bounded
composed
non-generative
```

Forget:

```text
provenance-aware
deterministic
synchronous
transitive
```

A arquitetura preserva a separação central do produto:

```text
Cognitive Memory
    = what is true

Episode
    = what happened

ContextCheckpoint
    = where we are

Session
    = what was said/done over time

Knowledge Memory
    = what sources tell us
```

O Sofias Memory permanece responsável por:

```text
persist
retrieve
version
trace
forget
```

e não passa a ser responsável por:

```text
plan
authorize
decide
execute
```
