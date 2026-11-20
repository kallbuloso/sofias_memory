# Sofias Memory — Implementation: SM-CTX-005 — Provenance-Aware Forget Propagation

**Document:** `implementation/13-SF-Implementation-SM-CTX-005-Provenance-Aware-Forget.md`\
**Status:** APPROVED\
**Date:** 2026-11-20\
**Project:** Sofias Memory\
**Current Slice:** `SM-CTX-005 — Provenance-Aware Forget Propagation`

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

---

# 1. Objective

Implementar:

```text id="5sgn66"
SM-CTX-005 — Provenance-Aware Forget Propagation
```

Ao final deste Slice, a garantia deverá ser:

> depois que uma operação de Forget reportar conclusão bem-sucedida, nenhum conteúdo contextual derivado que tenha perdido suporte suficiente por causa desse Forget poderá continuar sendo recuperado.

Fluxo conceitual:

```text id="2mvkut"
authoritative source
        │
        │ explicit Evidence
        ▼
derived Episode
        │
        │ explicit Evidence
        ▼
another Episode
        │
        ▼
ContextStatement
```

Quando o suporte autoritativo necessário desaparecer:

```text id="nzy8ng"
Forget
  ↓
re-evaluate explicit support
  ↓
scrub unsupported derived content
  ↓
propagate through Episode dependencies
  ↓
invalidate checkpoint when current_state disappears
```

Sem:

- blind cascade;
- text matching;
- provenance inference;
- LLM;
- eventual async cleanup.

---

# 2. Current Role

```text id="q631dy"
CURRENT ROLE: Senior Software Engineer
CURRENT PHASE: Implementation
CURRENT SLICE: SM-CTX-005
```

Não alterar decisões APPROVED.

---

# 3. Required Entry State

Antes de executar:

```text id="ik45mg"
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
SM-CTX-004 DONE
```

---

## 3.1 SM-CTX-004 MUST provide

```text id="48o04a"
contextual_evidence_refs
Episode -> Evidence links
ContextStatement -> Evidence links
EvidenceResolver
SupportEvaluator
all_required
any_sufficient
cycle prevention
current Evidence availability
```

---

# 4. Core Privacy Guarantee

A garantia pública é:

```text id="jbi112"
Forget completed successfully
    =>
unsupported derived contextual content is no longer recoverable
```

Não é aceitável:

```text id="o8c2kv"
Forget source
    ↓
source disappears
    ↓
derived Episode still exposes paraphrase of forgotten content
```

Isso seria semantic resurrection.

---

# 5. Explicit Evidence Only

Propagation utiliza exclusivamente:

```text id="is5b20"
EvidenceRef
```

Nunca:

- provenance;
- conversation_ref;
- source_system;
- textual similarity;
- same project;
- same Session;
- temporal proximity.

Regra congelada no CTX-004:

```text id="md7an6"
Provenance != Evidence
```

permanece absoluta.

---

# 6. No Blind Cascade

Forget de:

```text id="pzvb12"
Source A
```

não significa:

```text id="qfqzfk"
forget every Episode in the project
```

Somente owners cujo explicit support realmente perdeu suficiência podem ser alterados.

---

# 7. Propagation Is Support-Aware

Exemplo:

```text id="ghqll8"
Episode B
mode = any_sufficient
Evidence = [A, C]
```

Se A for forgotten e C continuar AVAILABLE:

```text id="tjqxt1"
B remains valid
```

Não scrubbar.

---

# 8. `all_required`

Se:

```text id="a4u6hs"
mode = all_required
Evidence = [A, B, C]
```

e qualquer Evidence deixar de ser AVAILABLE:

```text id="jrhzzt"
support insufficient
```

Derived content precisa convergir para estado não recuperável.

---

# 9. `any_sufficient`

Se pelo menos uma Evidence permanece AVAILABLE:

```text id="m7fhvl"
support sufficient
```

Se nenhuma permanece AVAILABLE:

```text id="a7w6y3"
support insufficient
```

---

# 10. Support-Loss States

Para avaliação de sufficiency:

```text id="c5ptgo"
AVAILABLE
    = usable support

UNAVAILABLE
    = not usable support

FORGOTTEN
    = not usable support
```

A diferença entre:

```text id="an6d2s"
unavailable
forgotten
```

continua importante para API/provenance, mas ambas deixam de satisfazer support.

---

# 11. Why `UNAVAILABLE` Also Matters

O Forget existente possui:

```text id="c6lz2c"
memory_only=true
```

Nesse fluxo, a Source original não vira `DELETED`.

Ela termina normalmente:

```text id="0kb56r"
PENDING
```

enquanto Documents/Chunks antigos deixam de ser authoritative.

Portanto:

```text id="c7zv77"
Forget(memory_only=true)
```

pode causar:

```text id="oo2j8a"
Evidence AVAILABLE
    ↓
UNAVAILABLE
```

sem chegar ao estado público `FORGOTTEN`.

Mesmo assim, o support foi perdido.

Propagation deve reagir.

---

# 12. No Resurrection After Re-cognify

Se memory-only Forget fizer:

```text id="qq4w1g"
Source ACTIVE
    ↓
PENDING
```

e depois Cognify tornar a Source utilizável novamente:

```text id="fjcr3c"
PENDING -> ACTIVE
```

conteúdo contextual scrubbed durante o Forget:

```text id="ac24gs"
MUST NOT resurrect
```

Não existe restore automático de derived Episode/ContextStatement.

---

# 13. Forget Integration Surfaces

Propagation deverá integrar com:

```text id="qm8mkm"
Cognitive Memory precise Forget

Episode direct Forget

Knowledge Source Forget

Knowledge Dataset Forget

Knowledge Forget Everything

Administrative Dataset Delete
```

Administrative Dataset Delete entra porque reutiliza a mesma authoritative Knowledge deletion primitive.

---

# 14. Not an Integration Surface

Não existe propagation trigger neste Slice para:

```text id="e5ls85"
Session archive
Skill archive
Agent archive
ordinary Episode invalidation
ordinary Cognitive Memory supersession
ordinary Knowledge generation replacement
```

Esses estados podem afetar availability conforme CTX-004, mas este Slice é especificamente o convergence contract de Forget/destructive knowledge removal.

---

# 15. SessionEntry

`session_entry` é EvidenceSourceType, mas Sessions atualmente não possuem destructive Forget/hard-delete público.

Portanto não há Forget hook de SessionEntry neste Slice.

Se futuramente surgir destructive SessionEntry deletion:

```text id="8w4vv5"
new integration decision required
```

---

# 16. `ContextualEvidenceInvalidator`

Criar service interno explícito, conceitualmente:

```text id="264l74"
ContextualEvidenceInvalidator
```

Responsabilidades:

```text id="ubn44u"
receive authoritative evidence-loss seeds
find contextual dependents
re-evaluate full support set
scrub unsupported Episodes
remove unsupported ContextStatements
invalidate checkpoints when required
propagate transitively through Episodes
```

---

# 17. Not a Public API

Não criar:

```http id="2daeq2"
POST /api/v1/context/evidence/invalidate
POST /api/v1/context/reconcile
POST /api/v1/context/propagate-forget
```

Propagation é parte do Forget authoritative lifecycle.

---

# 18. No Background Worker

Não criar:

- queue;
- worker;
- scheduled reconciliation;
- eventual consistency job.

Propagation é synchronous em relação ao authoritative PostgreSQL Forget boundary.

---

# 19. Meaning of Synchronous

Para direct synchronous Forget:

```text id="iyd3ak"
HTTP success
    =>
target Forget + contextual propagation committed
```

Para Knowledge pipeline Forget:

```text id="bkyqvr"
PipelineRun SUCCEEDED
    =>
contextual propagation was already durably committed
```

---

# 20. `202 Accepted` Is Not Completion

Knowledge Forget com:

```text id="607mxq"
wait=false
```

pode retornar:

```http id="ezmj25"
202 Accepted
```

Isso significa apenas:

```text id="aiuic4"
Forget accepted durably
```

Não significa:

```text id="gmz4nw"
Forget propagation complete
```

A garantia forte começa quando o run alcançar:

```text id="x9fvg0"
SUCCEEDED
```

---

# 21. `wait=true`

Quando o endpoint retorna successful completed result após esperar o run:

```text id="f08thl"
derived cleanup MUST already be complete
```

---

# 22. Existing Knowledge Forget Pipeline

O pipeline atual executa:

```text id="zmj38e"
1. authoritative_mutation
2. projection_convergence
3. storage_deletion
4. finalize_target
5. finalize_result
```

A integração contextual deve ocorrer no:

```text id="9nmwwn"
authoritative_mutation
```

e não em `finalize_result`.

---

# 23. Why `authoritative_mutation`

Esse step já:

- locks authoritative targets;
- desativa Documents/Chunks;
- altera Knowledge authoritative state;
- executa em PostgreSQL;
- possui transaction própria;
- não depende de filesystem;
- não depende de Neo4j.

Esse é o boundary correto.

---

# 24. Knowledge Integration Point

Integrar em:

```text id="qkixu1"
apply_source_forget_mutation(...)
apply_dataset_forget_mutation(...)
```

ou boundary imediatamente equivalente que permaneça dentro da mesma authoritative transaction.

---

# 25. Administrative Dataset Delete

`DatasetDelete` reutiliza:

```text id="bzbiq7"
apply_dataset_forget_mutation(...)
```

Portanto, a integração compartilhada deve garantir que administrative delete também remova suporte contextual derivado.

Não criar código duplicado no DatasetDelete pipeline.

---

# 26. Propagation Before External Work

Fluxo correto:

```text id="c4rdwb"
BEGIN authoritative PostgreSQL transaction

deactivate Knowledge authoritative content

propagate contextual support loss

persist all mutations

COMMIT

then:
Neo4j convergence
storage deletion
finalization
```

---

# 27. Stronger Than Final Success

Pode ocorrer:

```text id="c5wgxv"
authoritative mutation commits
contextual scrub commits
external later step fails
run = FAILED
```

Nesse caso, contextual scrub:

```text id="pb5m6z"
MAY already be permanent
```

Isso é compatível com Forget fail-safe semantics.

---

# 28. Failure Does Not Mean No Mutation Happened

Não prometer:

```text id="szo968"
FAILED Forget => nothing changed
```

O pipeline existente já possui durable step boundaries.

A garantia é unilateral:

```text id="gp2wa7"
SUCCEEDED => propagation complete
```

não:

```text id="df17z3"
FAILED => zero mutation
```

---

# 29. Why We Do Not Wait Until Finalization

Se contextual cleanup acontecesse apenas em `finalize_result`:

- forgotten Knowledge já teria sido committed anteriormente;
- uma failure intermediária poderia deixar derived context exposto;
- retry/recovery teria de reconstruir semantic cleanup.

Isso aumenta risco.

Não usar esse modelo.

---

# 30. Knowledge Evidence Seeds

Source-level Knowledge Forget deve considerar EvidenceRefs de tipos:

```text id="ogbwwv"
source
document
chunk
```

ligadas à Source alvo.

---

# 31. Do Not Seed Only Active Documents

A implementação não pode usar apenas:

```text id="04lapn"
documents/chunks touched by current active generation
```

para localizar dependentes.

Pode existir contextual Evidence apontando para uma row histórica/inativa ligada à Source.

Quando a Source inteira sofre destructive Forget:

```text id="r7yjbx"
all contextual refs structurally belonging to that Source
```

precisam ser considerados.

---

# 32. Efficient Knowledge Dependency Lookup

Preferir repository query sobre:

```text id="gxim8j"
contextual_evidence_refs
```

que encontre apenas canonical refs realmente existentes para:

```text id="ezn839"
source_id in affected Sources

document.source_id in affected Sources

chunk.source_id in affected Sources
```

Não carregar todos os Documents/Chunks do Dataset apenas para descobrir que nunca foram Evidence.

---

# 33. Dataset Forget

Dataset-scope Forget fornece conjunto de Sources.

Propagation deve cobrir EvidenceRefs relacionados a todas elas.

---

# 34. Forget Everything

Mesmo mecanismo:

```text id="09cptv"
all targeted Sources
    ↓
lookup contextual refs
    ↓
propagation
```

Não implementar um second algorithm.

---

# 35. Cognitive Memory Forget Integration

`CognitiveMemoryService.forget(...)` já executa destructive mutation em uma short PostgreSQL transaction.

Adicionar propagation na mesma transaction.

Fluxo:

```text id="5kdwtz"
claim/check Cognitive idempotency
lock MemoryItem

scrub MemoryItem
scrub provenance
flush if required

ContextualEvidenceInvalidator(memory_item, id)

persist result
commit
```

---

# 36. Cognitive Forget Atomicity

Se contextual propagation falhar:

```text id="0vbw4r"
Cognitive Forget MUST NOT commit successfully
```

Target Forget e propagation devem rollback juntos.

---

# 37. Episode Direct Forget Integration

`EpisodeService.forget(...)` deve seguir o mesmo princípio.

```text id="kjhj8t"
forget target Episode
    ↓
make target non-recoverable
    ↓
propagate Episode evidence loss
    ↓
commit once
```

---

# 38. Episode Forget Atomicity

Se propagation falhar:

```text id="05oei6"
direct Episode Forget MUST rollback
```

Não retornar successful Forget com dependents ainda expondo required content.

---

# 39. Checkpoint Direct Forget

`ContextCheckpoint` não é `EvidenceSourceType` v1.

Portanto direct checkpoint Forget:

- remove seus statements;
- remove outgoing statement support;
- não inicia inbound propagation para outros resources.

Não inventar checkpoint-as-Evidence.

---

# 40. Evidence Loss Does Not Mean Canonical Ref Deletion

Quando source é forgotten:

```text id="sppnu8"
contextual_evidence_refs
```

pode permanecer.

Ela contém apenas:

```text id="x92a1b"
source_type
source_id
```

e permite resolver:

```text id="rm1d2e"
FORGOTTEN
```

Não apagar canonical identity necessária para dependents.

---

# 41. Reverse Dependency Lookup

Repository deve conseguir localizar:

```text id="31c5hr"
EvidenceRef
    ↓
Episode owners

EvidenceRef
    ↓
ContextStatement owners
```

de forma determinística.

---

# 42. Owner Support Re-evaluation

Ao encontrar um owner:

```text id="a7fdvz"
do NOT assume owner unsupported just because one ref changed
```

Sempre carregar o support set completo atual e executar:

```text id="dwzqjw"
SupportEvaluator
```

---

# 43. `all_required` Example

```text id="6hhb6a"
Episode B
all_required [A, C]

A forgotten
C available
```

Resultado:

```text id="tzkjx2"
B unsupported
```

---

# 44. `any_sufficient` Example

```text id="2ilgnz"
Episode B
any_sufficient [A, C]

A forgotten
C available
```

Resultado:

```text id="isrleq"
B remains
```

---

# 45. Retained Forgotten Ref Under `any_sufficient`

Quando B permanece suportado:

```text id="2rq3hi"
B -> A forgotten
B -> C available
```

os links podem permanecer.

GET poderá mostrar:

```text id="xpsvmy"
A = forgotten
C = available
```

Isso é provenance estrutural, não exposição do conteúdo de A.

---

# 46. Unsupported Episode Action

Quando um Episode com explicit support deixa de ter suporte suficiente por causa da propagation:

```text id="e2tjyf"
semantic Episode payload MUST become unrecoverable
```

A ação escolhida para v1 é:

```text id="5m881x"
Episode -> FORGOTTEN tombstone
```

---

# 47. Why Not Only INVALIDATED

`INVALIDATED` normal preserva payload histórico.

Isso seria insuficiente para Forget propagation porque poderia manter:

```text id="j8tl12"
summary
situation
reason
outcome
```

derivados do conteúdo esquecido.

Logo, support loss causado por Forget exige destructive scrub.

---

# 48. Episode Propagation Scrub

Para derived Episode unsupported:

- scrub `scope` conforme tombstone semantics aprovadas;
- scrub `context_key`;
- scrub `summary`;
- scrub `situation`;
- scrub `reason`;
- scrub `outcome`;
- scrub embedding;
- scrub confidence;
- scrub occurred_at quando aprovado pelo tombstone;
- scrub invalidation reason;
- scrub external provenance refs;
- set lifecycle `FORGOTTEN`;
- set `forgotten_at`;
- clear/remove outgoing support links;
- set support mode null if persistence requires.

Preservar somente tombstone estrutural permitido.

---

# 49. Already INVALIDATED Episode

Se:

```text id="a41wxm"
Episode lifecycle = INVALIDATED
```

mas ainda retém semantic payload e agora perde required support por Forget:

```text id="y5pzlr"
INVALIDATED -> FORGOTTEN
```

com destructive scrub.

---

# 50. Already FORGOTTEN Episode

No-op de domínio.

Ainda pode servir como transitive seed se necessário durante convergence.

---

# 51. Episode Becomes a New Evidence-Loss Seed

Quando Episode B é scrubbed:

```text id="er47dv"
B -> FORGOTTEN
```

qualquer resource que declara:

```text id="o6gxhy"
EvidenceRef("episode", B.id)
```

precisa ser reavaliado.

Assim a propagation torna-se transitive.

---

# 52. Transitive Example

```text id="1iyph1"
Source A
   ↓
Episode B
   ↓
Episode C
   ↓
ContextStatement D
```

Se todos usam:

```text id="22l53k"
all_required
```

Forget A produz:

```text id="3xyoee"
A unavailable/forgotten
B FORGOTTEN
C FORGOTTEN
D removed
```

---

# 53. ContextStatement Unsupported Action

Quando um ContextStatement perde support suficiente:

```text id="on9fvj"
remove/scrub the ContextStatement
```

A estratégia preferida é:

```text id="t9jhpn"
DELETE statement row
```

porque statement identity não é tombstone público e seu conteúdo precisa desaparecer.

Outgoing Evidence links desaparecem via structural cascade.

---

# 54. Preserve Other Statements

Não apagar o checkpoint inteiro por causa de qualquer statement.

Exemplo:

```text id="e0dqyb"
objective       supported
current_state   supported
pending_work A  unsupported
pending_work B  supported
```

Resultado:

```text id="2mv4xf"
remove pending_work A only
```

Checkpoint permanece válido.

---

# 55. Preserve Ordinals

Quando collection statement é removido:

```text id="o8mahu"
do not renumber unaffected statements
```

Ordenação continua pelo ordinal original.

Gaps são aceitáveis.

Isso evita mutar identidade/história de statements não relacionados.

---

# 56. `current_state` Is Special

Se o unsupported statement é:

```text id="601oo3"
current_state
```

o checkpoint não pode continuar sendo considerado um snapshot válido.

---

# 57. ACTIVE Checkpoint Losing `current_state`

Fluxo:

```text id="8p4ge3"
ACTIVE checkpoint
    ↓
remove current_state
    ↓
lifecycle = INVALIDATED
invalidated_at = now
```

Depois:

```text id="rwumla"
GET /current
    -> CURRENT_CHECKPOINT_NOT_FOUND
```

---

# 58. No Historical Fallback

Nunca:

```text id="42zxhf"
ACTIVE invalidated by support loss
    ↓
previous SUPERSEDED revision becomes ACTIVE
```

Proibido.

---

# 59. SUPERSEDED Checkpoint Losing `current_state`

Resultado:

```text id="tgvs7e"
SUPERSEDED -> INVALIDATED
```

mas preservar:

```text id="g2ywh7"
superseded_at
superseded_by
```

Lineage histórico continua intacto.

---

# 60. Already INVALIDATED Checkpoint

Remover `current_state` se necessário.

Lifecycle permanece:

```text id="6u66ps"
INVALIDATED
```

---

# 61. FORGOTTEN Checkpoint

Já não deve possuir semantic statements relevantes.

No-op.

---

# 62. Derived Invalidation Reason

Não copiar Evidence content para:

```text id="a0k6w2"
invalidation_reason
```

Se o schema permitir nullable:

```text id="0l3yft"
leave derived invalidation_reason = null
```

é preferível.

Se uma razão estrutural for necessária:

```text id="snqs73"
"required evidence is no longer available"
```

pode ser usada como mensagem fixa.

Nunca incluir:

- forgotten source text;
- source title;
- Episode summary;
- Chunk excerpt.

---

# 63. Result Schema After Propagation

Create/Supersede requests continuam exigindo:

```text id="n9lr5v"
current_state
```

Mas um checkpoint histórico resultante de propagation pode legitimamente possuir:

```text id="081xo0"
current_state = null
```

quando foi invalidado pela perda desse statement.

Response schema deve suportar esse estado.

Isso não permite criar checkpoint sem `current_state`.

Testar GET/list e hydration de replay após propagation: INVALIDATED sem current_state é serializável e preserva stream/revision/lineage; Create/Supersede sem current_state continuam inválidos. Checkpoint FORGOTTEN preserva a identidade estrutural de `10` §44, removendo os statements semânticos.

---

# 64. Support-Null Owners

Resources com:

```text id="vd9rbh"
support = null
```

não participam da propagation.

Mesmo que provenance aponte para a source esquecida.

---

# 65. Existing Pre-Evidence Resources

Mesma regra:

```text id="8xbsok"
support absent
    =>
no explicit dependency
```

Não scrubbar por heurística.

---

# 66. Deterministic Traversal

Propagation deve usar ordering estável.

Exemplo:

```text id="bxw4xd"
source_type
source_id
owner_type
owner_id
```

ordenados deterministicamente.

Não depender da ordem natural do PostgreSQL.

---

# 67. DAG Assumption

CTX-004 impede cycles.

Logo, Evidence dependency graph esperado é DAG.

Propagation pode usar queue/topological traversal.

---

# 68. Runtime Cycle Defense

Mesmo assim, corruption manual ou bug antigo pode introduzir cycle.

Se traversal detectar cycle:

```text id="t0llnt"
FAIL CLOSED
```

Não:

- loop infinito;
- truncar propagation;
- marcar operação como success parcial.

---

# 69. Corrupt/Dangling Evidence

Se um link viola invariantes estruturais de modo que support não possa ser avaliado com segurança:

```text id="kd8rq3"
FAIL CLOSED
```

Forget não pode reportar sucesso enquanto semantic cleanup é indeterminado.

---

# 70. Dependency Operational Failure

Falha PostgreSQL/transient durante propagation:

```text id="3pyo3a"
operation fails
```

Não persistir:

```text id="iz1jng"
availability = unavailable
```

como substituto de erro operacional.

---

# 71. No LLM

Propagation nunca chama:

- chat completion;
- embedding;
- classifier;
- summarizer.

É puro:

```text id="qa33fx"
PostgreSQL + deterministic rules
```

---

# 72. No Neo4j

Traversal de contextual Evidence não usa Neo4j.

Mesmo quando Knowledge Forget também gera `graph_outbox`, esse é outro domínio.

---

# 73. No New `graph_outbox`

Contextual propagation em si:

```text id="fzbvqe"
creates zero graph_outbox events
```

Knowledge Forget continua criando seus próprios eventos normais.

Tests devem comparar com baseline, não exigir total zero quando o Knowledge operation naturalmente usa graph projection.

---

# 74. No PipelineRun for Derived Mutations

Os derived Episode/ContextStatement changes:

```text id="b8h831"
do not create new PipelineRun
```

Eles fazem parte da operação pai.

---

# 75. No Idempotency Ledger Entry Per Derived Resource

Não criar:

```text id="b2v8ex"
one contextual_memory_idempotency entry
for every cascade mutation
```

Derived changes são efeitos determinísticos da operação Forget pai.

---

# 76. Idempotency of the Invalidator

Executar o invalidator novamente sobre o mesmo support loss deve convergir para o mesmo estado.

Exemplo:

```text id="pu7j62"
B already FORGOTTEN
D statement already removed
```

segunda traversal:

```text id="uvgz98"
no additional semantic mutation
```

---

# 77. Direct Forget Replay

Same-key replay continua obedecendo o ledger da operação pai.

Um Forget replay concluído após CTX-005 MUST NOT retornar success antes de garantir propagation convergida para o target. Isso inclui outcomes antigos do período incremental CTX-004: convergence direcionada é idempotente e não é backfill global/startup. Ledger resolution continua primeiro; não criar nova operação lógica nem restaurar conteúdo antigo.

Não quebrar replay precedence.

---

# 78. New-Key Forget of Already-Forgotten Episode

Continua resource-state no-op para o target.

MUST garantir idempotent propagation convergence para:

```text id="2giogv"
EvidenceRef("episode", target.id)
```

antes de retornar.

Isso é particularmente útil para dados criados durante a fase incremental anterior à ativação completa do Contract.

---

# 79. No Automatic Historical Backfill

A Architecture APPROVED proíbe historical backfill automático.

SM-CTX-005 não deve:

```text id="7g57tt"
scan entire database on startup
and rewrite all contextual resources
```

---

# 80. Consequence for Contract Activation

Como os Slices são implantados incrementalmente antes de Contract v1 ser anunciado:

```text id="asq0g1"
SM-CTX-008
```

deverá verificar a integridade necessária antes de anunciar:

```text id="041mxv"
contextual_project_memory = "1"
```

Não anunciar Contract completo enquanto houver inconsistência conhecida.

---

# 81. Evidence Loss Discovery for Knowledge Sources

Criar repository primitive conceitual como:

```text id="au8tmr"
list_contextual_refs_for_knowledge_sources(source_ids)
```

que encontre canonical refs:

```text id="jht3zj"
source refs with source_id in source_ids

document refs whose Document.source_id in source_ids

chunk refs whose Chunk.source_id in source_ids
```

---

# 82. Why Query Canonical Refs

Isso evita varrer:

```text id="molqlg"
every Document
every Chunk
```

quando poucos foram usados como contextual Evidence.

---

# 83. MemoryItem Seed Lookup

Para precise Cognitive Forget:

```text id="pxy5p8"
("memory_item", memory_id)
```

Se canonical EvidenceRef não existe:

```text id="6i0p0i"
propagation no-op
```

---

# 84. Episode Seed Lookup

Para direct Episode Forget:

```text id="s8o7ji"
("episode", episode_id)
```

mesma regra.

---

# 85. Knowledge `memory_only=true`

Ao entrar no authoritative mutation:

```text id="cmyepw"
Source.status = DELETING
Documents/Chunks deactivated
```

Evidence resolver os verá como não disponíveis.

Propagation pode ocorrer imediatamente.

Depois Source finaliza:

```text id="tp4mca"
PENDING
```

---

# 86. Full Source Forget

No authoritative mutation:

```text id="li27jh"
Source = DELETING
Documents/Chunks inactive
```

Support já foi perdido.

Depois finalization:

```text id="avglxy"
Source = DELETED
```

Public Evidence availability então converge para:

```text id="5mu4zb"
FORGOTTEN
```

---

# 87. Support Decision Does Not Need Final Label

Propagation precisa saber:

```text id="1hcs7x"
is this Evidence still AVAILABLE?
```

Não precisa aguardar a distinção final:

```text id="5y6fuu"
UNAVAILABLE vs FORGOTTEN
```

para remover conteúdo cuja sufficiency já foi perdida.

---

# 88. Re-cognify Does Not Restore Old Document/Chunk Evidence

Novos authoritative Documents/Chunks possuem sua própria identidade.

EvidenceRef antiga continua apontando para o recurso antigo.

Não rebind automaticamente.

---

# 89. Source Evidence After Re-cognify

Mesmo se a mesma Source row voltar a ACTIVE posteriormente:

```text id="dwawhx"
derived contextual content scrubbed by prior Forget remains scrubbed
```

Não existe resurrection.

---

# 90. Propagation Algorithm — Conceptual

```text id="2wm5bw"
queue = authoritative evidence-loss seeds
visited = empty

while queue not empty:

    take next seed deterministically

    find Episode dependents
    find ContextStatement dependents

    for each Episode:
        re-evaluate entire support set
        if insufficient and not FORGOTTEN:
            scrub Episode -> FORGOTTEN
            enqueue ("episode", episode.id)

    for each ContextStatement:
        re-evaluate entire support set
        if insufficient:
            remove statement
            if statement_type == current_state:
                invalidate containing checkpoint as required
```

---

# 91. Re-evaluate, Do Not Trust Trigger

O fato de um owner ter sido encontrado via changed Evidence não significa que precise ser removed.

Sempre executar:

```text id="iibcq2"
full support evaluation
```

---

# 92. Avoid Reprocessing Owners

Traversal pode manter set de owners já avaliados por graph state/version dentro da transaction.

Mas se uma owner depende de múltiplos Episodes que mudam durante a mesma propagation, a implementação deve garantir que sua decisão final reflita o estado mais recente da transaction.

Não otimizar incorretamente.

---

# 93. Flush Boundaries

SQLAlchemy session possui autoflush desabilitado em partes do projeto.

Quando uma mutation precisa ser observada por resolver/query subsequente:

```text id="nr3fjg"
explicit uow.flush()
```

deve ser usado.

Isso é particularmente importante em cadeia:

```text id="ozz6bh"
Episode B -> FORGOTTEN
then resolve B as Evidence for C
```

---

# 94. Transaction Size

Propagation pode aumentar a transaction do Forget.

Isso é deliberado para cumprir o Contract forte.

Não quebrar atomicidade apenas para reduzir transaction size.

Se performance real demonstrar problema:

```text id="qblt52"
future architecture decision
```

---

# 95. Bounded Requests vs Unbounded Transitive Chain

Evidence sets individuais são bounded pelo CTX-004.

A cadeia transitive total pode ser maior.

Traversal deve:

- terminar pelo DAG;
- evitar recursion Python profunda;
- preferir iterative queue;
- não usar recursion ilimitada.

---

# 96. Episode Outgoing Support After Scrub

Ao transformar Episode em FORGOTTEN:

```text id="adv9e4"
remove outgoing Episode->Evidence links
support_mode = null
```

Isso minimiza retenção desnecessária.

---

# 97. Episode Inbound Evidence Identity Remains

Não apagar:

```text id="f1gi2a"
canonical EvidenceRef ("episode", episode.id)
```

apenas porque Episode foi forgotten.

Outros owners precisam resolver esse source como:

```text id="2c0oao"
FORGOTTEN
```

---

# 98. ContextStatement Outgoing Links

Ao remover ContextStatement:

```text id="5xu2po"
ContextStatement -> Evidence links
```

podem desaparecer por `ON DELETE CASCADE`.

---

# 99. Canonical Evidence GC

Não executar GC agressivo durante propagation.

Canonical refs órfãs podem permanecer.

GC não é correctness requirement do Slice.

---

# 100. Checkpoint Statement Collections

Quando uma collection perde um item:

```text id="b1l36p"
pending_work:
ordinal 0 kept
ordinal 1 removed
ordinal 2 kept
```

response ordenada:

```text id="s4dpfb"
ordinal 0
ordinal 2
```

Sem renumber.

---

# 101. Current Checkpoint With Non-current Statement Loss

Exemplo:

```text id="pd96va"
current_state remains supported
blocker removed
```

Checkpoint permanece:

```text id="p4mvam"
ACTIVE
```

---

# 102. Current Checkpoint With `current_state` Loss

Checkpoint torna-se:

```text id="qjtwf6"
INVALIDATED
```

e deixa de ser current.

---

# 103. Recovery After Propagated Invalidation

Depois, Consumer pode usar:

```http id="9smzh2"
POST /api/v1/context/checkpoints
```

para criar novo current conforme CTX-002 amendment.

Revision:

```text id="syugmh"
MAX(revision) + 1
```

Nenhum historical fallback.

---

# 104. Superseded Checkpoint Partial Scrub

Historical revision pode perder um unsupported statement e continuar:

```text id="0ag7p9"
SUPERSEDED
```

desde que `current_state` continue presente/supported.

Se `current_state` for removido:

```text id="ly72zw"
SUPERSEDED -> INVALIDATED
```

preservando lineage.

---

# 105. Forget of Episode Used by `any_sufficient`

Se dependente possui outra Evidence AVAILABLE:

```text id="16ecbd"
dependent remains
```

Isso é uma feature, não vazamento.

Caller declarou que qualquer Evidence individual era suficiente para sustentar a claim completa.

---

# 106. No Partial Text Scrubbing

Não tentar remover frases dentro de:

```text id="l1i2p8"
Episode.summary
ContextStatement.text
```

com LLM ou heurística.

A unidade de propagation é:

```text id="bb4u1j"
whole Episode
whole ContextStatement
```

---

# 107. Why Whole-Unit Scrub

Evidence support está associado à unidade semântica inteira.

Sem field-level Evidence dentro do Episode:

```text id="zqo91u"
partial semantic retention cannot be proven safe
```

Logo, fail closed.

---

# 108. Knowledge Source Full Forget Test

Criar:

```text id="u0f0m6"
Source S
Document D
Chunk C

Episode ES supported by S
Episode ED supported by D
Episode EC supported by C
```

Executar full Source Forget até SUCCEEDED.

Esperado:

```text id="npnhwo"
ES not recoverable semantically
ED not recoverable semantically
EC not recoverable semantically
```

---

# 109. Knowledge `memory_only` Test

Mesmo setup.

Executar:

```text id="jg1fln"
memory_only=true
```

Esperado:

```text id="9owwm0"
support loss propagated
derived unsupported contextual content scrubbed
```

Mesmo que Source seja preservada para re-cognify.

---

# 110. Re-cognify Non-resurrection Test

Após memory-only Forget:

```text id="wxfqz7"
re-cognify Source
```

Confirmar:

```text id="ou714e"
old derived Episodes remain FORGOTTEN
removed ContextStatements do not return
```

---

# 111. Cognitive Memory Forget Test

```text id="8h7x7n"
MemoryItem M
Episode B supported by M
```

Forget M.

Esperado na mesma committed operation:

```text id="586a52"
M FORGOTTEN
B FORGOTTEN
```

---

# 112. Cognitive Forget Rollback Test

Injetar failure no invalidator.

Esperado:

```text id="4aovsd"
M remains pre-Forget state
B remains unchanged
HTTP operation fails
```

---

# 113. Direct Episode Forget Test

```text id="jeuf1s"
Episode A
Episode B supported by A
```

Forget A.

Esperado:

```text id="80itvz"
A FORGOTTEN
B FORGOTTEN
```

same transaction.

---

# 114. Episode Forget Rollback Test

Falha no cascade:

```text id="qa88is"
target + dependents rollback
```

---

# 115. `any_sufficient` Preservation Test

```text id="jlmwtc"
Episode B
any_sufficient [A, C]
```

Forget A, C remains available.

Expected:

```text id="dgh5q1"
B ACTIVE
B content intact
```

---

# 116. `all_required` Cascade Test

```text id="zvbzdk"
B all_required [A,C]
```

Forget A.

Expected:

```text id="fgi0fp"
B FORGOTTEN
```

---

# 117. Multi-hop Test

```text id="0li1gx"
A source
B episode <- A
C episode <- B
D episode <- C
```

Forget A.

Expected:

```text id="r0gc88"
B,C,D FORGOTTEN
```

---

# 118. ContextStatement Non-current Test

```text id="3p30jj"
current_state supported independently
pending_work X supported by A
```

Forget A.

Expected:

```text id="x9vu2z"
pending_work X removed
checkpoint stays ACTIVE
```

---

# 119. ContextStatement `current_state` Test

```text id="6x07ej"
ACTIVE checkpoint.current_state supported solely by A
```

Forget A.

Expected:

```text id="p1i8vq"
current_state removed
checkpoint INVALIDATED
GET /current -> 404
```

---

# 120. No Fallback Test

Existing:

```text id="qkwrwn"
rev1 SUPERSEDED
rev2 ACTIVE
```

rev2 loses current_state.

Expected:

```text id="x5p07s"
rev2 INVALIDATED
no current
rev1 remains SUPERSEDED
```

---

# 121. Historical `current_state` Loss Test

rev1 already SUPERSEDED loses required support.

Expected:

```text id="0tku13"
rev1 INVALIDATED
rev1.superseded_by preserved
current revision unaffected
```

---

# 122. Provenance-Only Test

Episode has:

```text id="hpyl84"
conversation_ref points to forgotten-related interaction
support = null
```

Forget unrelated Knowledge source.

Expected:

```text id="oeepg0"
Episode unchanged
```

---

# 123. Canonical Evidence Integrity Test

Propagation does not delete canonical Evidence identity needed by inbound dependents.

---

# 124. Corrupt Cycle Test

Bypass normal API in controlled integration test and create cycle manually.

Forget a source entering that graph.

Expected:

```text id="ka0zdu"
operation fails closed
no success response
```

---

# 125. Knowledge Pipeline Failure After Authoritative Mutation

Force failure after:

```text id="c1l1zi"
authoritative_mutation succeeded
```

but before pipeline completion.

Expected:

```text id="6pxqux"
contextual scrub already committed
run may fail
```

Document this explicitly.

---

# 126. Knowledge Successful Completion Guarantee

When run reaches:

```text id="bx3nhj"
SUCCEEDED
```

assert no unsupported contextual owner remains recoverable.

---

# 127. Administrative Dataset Delete Test

Seed contextual Evidence pointing into Dataset.

Execute supported administrative Dataset Delete.

Expected:

```text id="5gl7fq"
contextual propagation occurs through shared authoritative mutation
```

Não duplicar algorithm.

---

# 128. Dataset Forget Test

Dataset with multiple Sources and contextual dependencies across them.

After SUCCEEDED:

```text id="oz1wqt"
every unsupported derived owner is converged
```

---

# 129. Forget Everything Test

Same guarantee across all affected Datasets.

---

# 130. No Additional Graph Events Test

Compare Knowledge Forget baseline graph event count versus same Knowledge Forget with contextual dependents.

Contextual propagation:

```text id="9i8okn"
adds 0 graph_outbox events
```

Knowledge baseline events continuam normais.

---

# 131. No Derived Pipeline Test

Count PipelineRuns before/after contextual cascade.

Only parent Knowledge Forget run may exist.

Derived Episode/Statement cleanup:

```text id="mmyk37"
creates zero additional PipelineRuns
```

---

# 132. No Derived Idempotency Entries

Cascade does not generate artificial `Idempotency-Key` records per dependent.

---

# 133. Restart Non-resurrection Test

Forget + cascade.

Restart application.

Confirm:

```text id="qw76e8"
forgotten Episodes remain tombstones
removed statements remain absent
invalidated checkpoint remains without current_state
no current fallback
```

---

# 134. Concurrency — Direct Episode Forget vs Read

Normal PostgreSQL transaction visibility applies.

After Forget commit:

```text id="gg0q7k"
subsequent read must not expose unsupported derived content
```

Não prometer snapshot isolation behavior para read iniciado antes do commit.

---

# 135. Concurrency — Two Forget Operations

Dois independent Forget operations que convergem sobre os mesmos dependents devem ser race-safe.

Final state:

```text id="l2n82r"
one deterministic scrubbed state
```

No duplicate semantic mutation.

---

# 136. Locking Strategy

Não criar global lock.

Usar:

- target row locks;
- deterministic owner ordering;
- existing PostgreSQL transaction semantics.

Se múltiplos owners precisarem lock:

```text id="29nfvt"
lock in deterministic UUID order
```

para reduzir deadlock risk.

---

# 137. Deadlock Handling

Não esconder deadlock com blind infinite retry.

Se o projeto já possui retry policy adequada para transient PostgreSQL serialization/deadlock, reutilizar apenas quando semanticamente seguro.

Caso contrário, falhar e permitir caller/pipeline retry controlado.

---

# 138. Public Errors

Direct Forget deve continuar usando seu Contract normal.

Não expor detalhes do dependency graph em error message.

Internal corruption pode virar:

```text id="6la19a"
INTERNAL_ERROR
```

ou stable internal error já aprovado.

Knowledge pipeline pode usar PermanentPipelineStepError sanitizado.

---

# 139. Observability

`ContextualEvidenceInvalidator` deve retornar contagens seguras, por exemplo:

```text id="4ybh69"
episodes_forgotten
statements_removed
checkpoints_invalidated
evidence_sources_processed
```

Pode ser usado em logs/metrics.

Não incluir semantic text.

---

# 140. Knowledge Run Metrics

É aceitável adicionar counters contextuais aos metrics de Forget de forma aditiva, se coerente com o padrão existente.

Não é Acceptance Criterion obrigatório.

---

# 141. Expected File Impact

Prováveis novos arquivos:

```text id="124mtm"
sofias_memory/services/contextual_evidence_invalidator.py
```

ou integração no módulo contextual Evidence existente.

Alterações esperadas:

```text id="a1xeck"
sofias_memory/services/cognitive_memory.py

sofias_memory/services/episodes.py

sofias_memory/services/forget.py

sofias_memory/pipelines/steps/forget.py
    only if required by integration boundary

sofias_memory/infrastructure/postgres/repositories/contextual_evidence.py

sofias_memory/infrastructure/postgres/repositories/context_checkpoints.py

sofias_memory/infrastructure/postgres/unit_of_work.py

checkpoint/episode schemas if partial-invalidated result requires it

tests
```

Administrative Dataset Delete deve preferencialmente ser coberto pela shared mutation sem duplicação.

---

# 142. Migration Impact

Idealmente:

```text id="j7ewx0"
no new table required
```

SM-CTX-004 deve ter criado a estrutura de Evidence.

Uma migration só é aceitável se necessária para:

- constraint correta;
- response/lifecycle-compatible persistence;
- missing index necessário para reverse dependency lookup.

Não criar schema novo por conveniência.

---

# 143. Suggested Indexes

Se ainda não existirem, reverse lookup precisa de index eficiente em:

```text id="33l98c"
episode_evidence_refs.evidence_ref_id

context_statement_evidence_refs.evidence_ref_id
```

Canonical lookup:

```text id="gbn001"
(source_type, source_id)
```

já deve ser unique/indexed.

---

# 144. No Trigger-Based Cascade

Não implementar propagation em PostgreSQL trigger.

Motivos:

- support evaluation precisa consultar múltiplas refs;
- lifecycle rules são domínio;
- behavior precisa ser testável no service;
- trigger esconderia semantics críticas.

---

# 145. Human Validation

Executar no ambiente real.

## Step A — Create chain

Criar:

```text id="1hjqpp"
Source S
Episode A supported by S
Episode B supported by A
Checkpoint.current_state supported by B
```

---

## Step B — Verify before Forget

Confirmar:

```text id="e85f9w"
A ACTIVE
B ACTIVE
checkpoint ACTIVE/current
```

---

## Step C — Full Forget S

Executar Forget com completion observada.

---

## Step D — Verify propagation

Esperado:

```text id="0dq4t1"
A FORGOTTEN
B FORGOTTEN
current_state removed
checkpoint INVALIDATED
GET current -> 404
```

---

## Step E — Restart

Reiniciar.

Revalidar tudo.

---

## Step F — `any_sufficient`

Criar novo chain onde derived Episode possui:

```text id="bo6hkm"
any_sufficient [S1, S2]
```

Forget apenas S1.

Confirmar derived Episode permanece.

---

## Step G — memory-only

Criar source/context chain.

Executar:

```text id="l4rjbt"
memory_only=true
```

Confirmar cleanup.

Re-cognify.

Confirmar nenhuma resurrection.

---

## Step H — Cognitive Memory

Criar MemoryItem M e Episode dependente.

Forget M.

Confirmar atomic propagation.

---

## Step I — Episode direct

Criar A -> B.

Forget A.

Confirmar B scrubbed no mesmo success boundary.

---

## Step J — Regression

Confirmar:

- no provenance-only cascade;
- Sessions permanecem intactas;
- Knowledge pipeline continua recuperável;
- graph_outbox continua apenas com eventos Knowledge normais;
- nenhum derived PipelineRun;
- Cognitive Memory idempotency continua correta;
- Episode idempotency continua correta.

---

# 146. Acceptance Criteria

## AC-005.1

Forget de source Evidence reavalia dependentes.

## AC-005.2

Forget de MemoryItem Evidence reavalia dependentes.

## AC-005.3

Direct Forget de Episode reavalia dependentes.

## AC-005.4

`all_required` perde suporte quando qualquer required Evidence deixa de estar AVAILABLE.

## AC-005.5

`any_sufficient` preserva derived content enquanto pelo menos uma Evidence permanece AVAILABLE.

## AC-005.6

Unsupported Episode deixa de ser semanticamente recuperável.

## AC-005.7

Unsupported ContextStatement é removido/scrubbed.

## AC-005.8

Perda de `current_state` invalida checkpoint.

## AC-005.9

Invalidar current checkpoint por support loss não reativa histórico.

## AC-005.10

Propagation é transitive através de Episode Evidence.

## AC-005.11

Propagation não usa LLM.

## AC-005.12

Propagation não usa Neo4j.

## AC-005.13

Propagation não cria PipelineRun derivado.

## AC-005.14

Propagation não cria graph_outbox contextual.

## AC-005.15

Após successful Forget, derived unsupported content não reaparece em read normal.

---

# 147. Additional Acceptance Criteria

## AC-005.16

Provenance sem Evidence explícita não causa cascade.

## AC-005.17

Session archive não dispara propagation.

## AC-005.18

Knowledge `memory_only=true` provoca support-loss convergence quando aplicável.

## AC-005.19

Re-cognify não ressuscita conteúdo contextual scrubbed.

## AC-005.20

Knowledge Forget integra propagation no authoritative PostgreSQL mutation boundary.

## AC-005.21

Administrative Dataset Delete reutiliza o mesmo propagation path.

## AC-005.22

Cognitive Memory precise Forget + propagation são atomicamente commitados.

## AC-005.23

Episode direct Forget + propagation são atomicamente commitados.

## AC-005.24

Runtime Evidence corruption/cycle impede successful Forget.

## AC-005.25

SUPERSEDED historical checkpoint que perde `current_state` vira INVALIDATED preservando lineage.

## AC-005.26

Non-current unsupported statement pode ser removido sem invalidar checkpoint.

## AC-005.27

Repeated propagation converge idempotentemente.

## AC-005.28

Knowledge Forget `202 Accepted` não é tratado como completion guarantee.

## AC-005.29

Pipeline `SUCCEEDED` implica propagation já durável.

## AC-005.30

Failure após authoritative mutation pode preservar scrub já committed e isso é documentado/testado.

---

# 148. Required Test Commands

Executar:

```bash id="bhxyc0"
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory
uv run pytest
```

mais suites PostgreSQL opt-in aplicáveis.

Se houver migration:

```bash id="3e61md"
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Não declarar PASS de teste não executado.

---

# 149. Findings Classification

## BLOCKER

Exemplos:

- successful Forget deixa unsupported semantic content recuperável;
- propagation depende de provenance implícita;
- `any_sufficient` vira blind cascade;
- current checkpoint faz fallback histórico;
- source Forget ignora Document/Chunk refs históricos ligados à Source;
- propagation ocorre apenas depois de pipeline success;
- target Forget commits mesmo quando required propagation falha;
- cycle/corruption é silenciosamente ignorado.

## IMPORTANT

Afeta garantia de privacy/correctness deste Slice.

## MINOR

Não impede a garantia principal.

## DEFERRED

Pertence a Recall/Hardening ou feature futura.

---

# 150. When to Stop and Escalate

Voltar para decisão humana se surgir necessidade de:

- usar LLM para decidir propagation;
- inferir Evidence por provenance;
- adicionar background cleanup;
- enfraquecer successful Forget guarantee;
- criar automatic historical fallback;
- alterar support modes;
- adicionar new EvidenceSourceType;
- tratar checkpoint inteiro como uma única Evidence unit;
- manter unsupported Episode content por conveniência;
- criar Neo4j dependency;
- criar distributed transaction;
- criar historical backfill automático.

---

# 151. Implementation Order

Ordem recomendada:

```text id="w2uqt0"
1. verify CTX-001..004 completion
2. inspect actual Evidence schema/resolvers
3. reverse dependency repository queries
4. ContextualEvidenceInvalidator
5. deterministic traversal
6. unsupported Episode scrub
7. ContextStatement scrub
8. checkpoint current_state invalidation
9. transitive Episode propagation
10. Cognitive Memory Forget integration
11. Episode Forget integration
12. Knowledge source mutation integration
13. dataset/everything coverage
14. Dataset Delete shared coverage
15. failure/rollback tests
16. memory_only/re-cognify tests
17. concurrency tests
18. no-graph/no-derived-pipeline assertions
19. restart/non-resurrection
20. Human Validation
```

---

# 152. Slice Exit Criteria

SM-CTX-005 estará pronto para closeout quando:

```text id="q98s2e"
AC-005.1..30 satisfied

direct Cognitive Forget strong guarantee proven
direct Episode Forget strong guarantee proven
Knowledge Forget strong guarantee proven
memory_only behavior proven
Dataset/Delete scopes proven
transitive cascade proven
any_sufficient preservation proven
current_state invalidation proven
no historical fallback proven
failure semantics documented/proven
no LLM
no Neo4j
no derived PipelineRun
no contextual graph_outbox
no BLOCKER remains
```

---

# 153. Expected Implementation Report

O executor deverá retornar:

```text id="rkgjcc"
SM-CTX-005 — Implementation Report

Implemented
- ...

Changed files
- ...

Database
- previous migration head:
- new migration head:
- schema/index changes:

Dependencies verified
- SM-CTX-001:
- SM-CTX-002:
- SM-CTX-003:
- SM-CTX-004:

Integration points
- Cognitive Memory Forget:
- Episode Forget:
- Source Forget:
- Dataset Forget:
- Forget Everything:
- Dataset Delete:

Propagation semantics
- Episode unsupported action:
- ContextStatement unsupported action:
- current_state loss:
- transitive strategy:
- locking/order strategy:

Verified
- unit:
- migration:
- PostgreSQL integration:
- direct MemoryItem Forget:
- direct Episode Forget:
- source Forget:
- dataset Forget:
- Forget Everything:
- Dataset Delete:
- memory_only:
- re-cognify non-resurrection:
- all_required:
- any_sufficient:
- multi-hop:
- current_state invalidation:
- no fallback:
- failure after authoritative mutation:
- corruption/cycle fail-closed:
- restart:
- graph isolation:
- derived pipeline isolation:

Acceptance Criteria
- AC-005.1 PASS/FAIL
...
- AC-005.30 PASS/FAIL

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

Não avançar automaticamente para SM-CTX-006.

---

# 154. Handoff to Executor

## Objective

Implementar:

```text id="e1hkjy"
SM-CTX-005 — Provenance-Aware Forget Propagation
```

## Required entry conditions

```text id="pqnkf8"
SM-CTX-001 DONE
SM-CTX-002 DONE
SM-CTX-003 DONE
SM-CTX-004 DONE
```

## Core guarantee

```text id="5atfbm"
successful Forget
    =>
no unsupported derived contextual semantic content remains recoverable
```

## Scope

```text id="jcocb6"
ContextualEvidenceInvalidator
reverse dependency lookup
deterministic support re-evaluation
Episode destructive support-loss scrub
ContextStatement support-loss scrub
checkpoint invalidation on current_state loss
transitive Episode cascade

Cognitive Memory Forget integration
Episode Forget integration
Knowledge Forget integration
Dataset Delete integration
```

## Out of Scope

```text id="yiyc8i"
Contextual Recall
automatic Evidence inference
LLM support evaluation
historical backfill
background reconciliation
Neo4j contextual graph
automatic historical fallback
```

## Expected result

Ao final:

> Forget deixa de ser apenas uma operação sobre o recurso explicitamente alvo e passa a respeitar o grafo de dependência que o próprio caller declarou através de Evidence — removendo somente aquilo que realmente perdeu suporte suficiente, preservando conteúdo sustentado independentemente e impedindo semantic resurrection.
