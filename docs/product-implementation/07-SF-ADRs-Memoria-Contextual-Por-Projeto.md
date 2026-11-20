# Sofias Memory — Architectural Decision Records: Memória Contextual Persistente por Projeto

**Document:** `07-SF-ADRs-Memoria-Contextual-Por-Projeto.md`\
**Status:** APPROVED\
**Date:** 2026-11-18\
**Project:** Sofias Memory\
**Baseline:**
- `01-SF-Discovery-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `03-SF-PRD-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `04-SF-Spec-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `05-SF-Contract-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `06-SF-Architecture-Memoria-Contextual-Por-Projeto.md` — APPROVED

---

# ADR-0017 — First-Class Contextual Memory Domain and PostgreSQL Authority

**Status:** APPROVED\
**Date:** 2026-11-18

---

## 1. Context

Sofias Memory já possui domínios first-class distintos para:

- Knowledge Memory;
- Sessions;
- Skills;
- Agent Management;
- Native Cognitive Memory.

A evolução de memória contextual persistente introduz necessidades que não pertencem adequadamente a nenhum desses modelos existentes.

É necessário representar:

```text id="12tuqs"
Episode
    = experiência histórica relevante

ContextCheckpoint
    = estado de continuidade de um contexto duradouro
```

Esses conceitos possuem lifecycle e comportamento diferentes de:

```text id="ybs8d4"
MemoryItem
SessionEntry
Document
Source
```

### Episode

Um Episode registra que algo aconteceu.

Exemplo:

```text id="f422df"
"Em novembro foi decidido utilizar Episode
como agregado first-class."
```

Essa experiência continua historicamente verdadeira mesmo se decisões posteriores modificarem a arquitetura.

Isso não corresponde à semântica de `MemoryItem.semantic`, cujo lifecycle inclui representação de verdade semântica vigente e supersession.

### ContextCheckpoint

Um ContextCheckpoint representa:

```text id="0grtav"
onde um trabalho está agora
```

e possui características próprias:

- revision;
- current state;
- supersession;
- concurrency;
- continuation state.

Também não corresponde à semântica de Session.

Uma Session é uma fronteira temporal de interação.

Um checkpoint pode atravessar múltiplas Sessions.

---

## 2. Decision

Sofias Memory introduzirá um domínio first-class denominado conceitualmente:

```text id="3zcr47"
Contextual Project Memory
```

composto inicialmente por:

```text id="pouduj"
Episode
Context Stream
ContextCheckpoint
ContextStatement
Evidence
Contextual Recall
```

---

## 2.1 Episode é agregado próprio

`Episode` não será modelado como:

```text id="pfzjcj"
MemoryItem(memory_type="episodic")
```

O contrato existente permanece:

```text id="by0zgl"
MemoryItem = profile | semantic
```

Episode possuirá:

- identidade própria;
- lifecycle próprio;
- provenance;
- Evidence;
- recuperação semântica própria.

---

## 2.2 ContextCheckpoint é agregado próprio

ContextCheckpoint não será armazenado como:

- Session metadata;
- SessionEntry especial;
- MemoryItem;
- Dataset;
- Source;
- Document.

Ele possuirá:

- identidade própria;
- scope;
- context stream;
- revision;
- lifecycle;
- statements;
- lineage.

---

## 2.3 PostgreSQL é autoridade

Todo estado autoritativo de Contextual Project Memory será armazenado em:

```text id="5f4mlm"
PostgreSQL + pgvector
```

Isso inclui, no mínimo:

- Episodes;
- Episode lifecycle;
- Episode embeddings;
- ContextCheckpoints;
- ContextStatements;
- revisions;
- current state;
- Evidence references;
- Evidence availability;
- idempotency;
- provenance;
- Forget state.

Essa decisão estende, sem alterar, o princípio do ADR-0002:

> PostgreSQL permanece a fonte de verdade do Sofias Memory.

---

## 2.4 Neo4j não participa do MVP contextual

No primeiro contrato:

```text id="l4v44i"
Episode
ContextCheckpoint
ContextStatement
Evidence
```

não serão projetados para Neo4j.

Nenhum estado contextual necessário para:

- recall;
- lifecycle;
- Forget;
- recovery;

poderá existir exclusivamente em Neo4j.

---

## 2.5 Contextual writes serão síncronos

As operações contextuais:

```text id="t5ywmk"
Episode create
Episode invalidate
Episode forget

Checkpoint create
Checkpoint supersede
Checkpoint invalidate
Checkpoint forget
```

serão operações síncronas e transacionais.

Não utilizarão `PipelineRun` como mecanismo normal de execução.

---

## 2.6 Contextual Recall será síncrono

Contextual Recall será uma operação de leitura síncrona e composta.

Ele poderá reutilizar:

- Cognitive Recall;
- Episode retrieval;
- checkpoint lookup;
- Knowledge Recall;
- Evidence expansion.

Não criará um novo corpus duplicado.

---

## 2.7 Não haverá backfill automático

A introdução do domínio contextual não converterá automaticamente dados existentes:

```text id="r7i4c4"
SessionEntry -> Episode
Session -> ContextCheckpoint
MemoryItem -> Episode
metadata -> ContextCheckpoint
```

Contextual Project Memory passará a existir explicitamente a partir da nova capability.

---

## 3. Rationale

### 3.1 Separação semântica

Os conceitos possuem perguntas diferentes:

```text id="ms2jtz"
MemoryItem
    -> O que é verdade agora?

Episode
    -> O que aconteceu?

ContextCheckpoint
    -> Onde estamos?

Session
    -> O que ocorreu temporalmente durante uma interação?

Knowledge Memory
    -> O que nossas fontes documentais dizem?
```

Fundir esses modelos reduziria clareza e criaria lifecycle ambíguo.

---

### 3.2 Preservação de contratos existentes

Criar novos agregados evita alterar silenciosamente:

- Native Cognitive Memory v0.7.0;
- Sessions;
- Knowledge Memory.

---

### 3.3 PostgreSQL já resolve as necessidades autoritativas

A capability necessita:

- transações;
- constraints;
- row locking;
- provenance;
- vector retrieval;
- recursive dependency traversal;
- durable lifecycle.

PostgreSQL + pgvector já atende a essas necessidades.

Não existe requisito demonstrado que justifique novo datastore.

---

### 3.4 Neo4j não agrega valor suficiente no primeiro incremento

O MVP não depende de:

- graph traversal complexo;
- causal graph global;
- Episode-to-Episode graph search.

Projetar os novos recursos no Neo4j adicionaria:

- outbox events;
- reconciliation;
- rebuild;
- schemas adicionais;
- Forget complexity;
- testes adicionais.

sem benefício necessário para provar continuidade contextual.

---

### 3.5 Mutations são pequenas e exigem resultado imediato

Episode e Checkpoint são writes relativamente pequenos.

O caller precisa saber imediatamente:

- se a operação foi aceita;
- qual recurso foi criado;
- qual é o current checkpoint;
- se houve conflict.

Pipeline assíncrono seria complexidade sem ganho funcional.

---

## 4. Alternatives Considered

### Alternative A — Adicionar `episodic` a MemoryItem

**Rejected.**

Vantagem:

- reutilizaria parte da infraestrutura cognitiva.

Problemas:

- altera contrato v0.7.0;
- mistura historical experience com current semantic truth;
- `superseded` não representa corretamente acontecimentos históricos;
- dificulta queries e lifecycle futuros.

---

### Alternative B — Usar SessionEntry como Episode

**Rejected.**

SessionEntry é:

```text id="rxsiei"
temporal interaction context
```

Episode é:

```text id="okjknk"
selected durable historical experience
```

Transformá-los no mesmo recurso faria toda interação carregar implicitamente valor de longo prazo.

---

### Alternative C — Armazenar checkpoint como metadata de Session

**Rejected.**

Uma linha de continuidade pode atravessar diversas Sessions.

O estado não pode pertencer exclusivamente à última conversa.

---

### Alternative D — Representar projeto como Dataset

**Rejected.**

Dataset representa corpus de Knowledge Memory.

Project Context possui semântica diferente e pode utilizar múltiplos Datasets.

---

### Alternative E — Neo4j como store principal de Episodes

**Rejected.**

Contraria ADR-0002 e dificultaria:

- transações;
- Forget;
- idempotência;
- recovery.

---

### Alternative F — Criar novo banco vetorial

**Rejected.**

pgvector já está disponível e atende ao MVP.

---

### Alternative G — Usar PipelineRun para writes contextuais

**Rejected.**

Adicionaria:

- estados intermediários;
- worker dependency;
- recovery adicional;

a operações que precisam de semântica síncrona.

---

### Alternative H — Backfill automático

**Rejected.**

Inferir Episodes e Checkpoints de dados históricos poderia criar:

- falsa provenance;
- experiências inexistentes;
- conclusões erradas;
- continuidade inventada.

---

## 5. Consequences

### Positive

- domínio contextual semanticamente claro;
- Native Cognitive Memory permanece compatível;
- Sessions permanecem compatíveis;
- PostgreSQL continua sendo única autoridade;
- nenhuma infraestrutura nova;
- feature pode evoluir independentemente;
- restart/recovery permanecem simples;
- futura projeção em grafo continua possível;
- writes contextuais permanecem previsíveis.

### Negative

- novos models e tabelas;
- novas migrations;
- novos services;
- nova API pública;
- alguma duplicação conceitual entre conteúdo de Episode e MemoryItem pode existir;
- Contextual Recall passa a coordenar múltiplos domínios.

### Trade-offs

Aceitamos mais tipos first-class em troca de:

```text id="dvsgpa"
semântica explícita
+
lifecycle correto
+
menor acoplamento entre domínios
```

---

## 6. Compatibility / Migration Impact

A decisão é aditiva.

Não altera schemas públicos de:

- Sessions;
- Cognitive Memory;
- Knowledge Memory.

Serão necessárias migrations PostgreSQL novas.

Não haverá migration Neo4j.

Não haverá backfill obrigatório.

Consumidores existentes continuarão funcionando sem conhecer Contextual Project Memory.

---

## 7. References

- ADR-0002 — PostgreSQL Source of Truth and Neo4j Rebuildable Projection
- ADR-0012 — First-Class Durable Sessions
- ADR-0016 — Native Cognitive Memory Model and Lifecycle
- `03-SF-PRD-Memoria-Contextual-Por-Projeto.md`
- `04-SF-Spec-Memoria-Contextual-Por-Projeto.md`
- `05-SF-Contract-Memoria-Contextual-Por-Projeto.md`
- `06-SF-Architecture-Memoria-Contextual-Por-Projeto.md`

---

# ADR-0018 — ContextCheckpoint Revision and Concurrency Model

**Status:** APPROVED\
**Date:** 2026-11-18

---

## 1. Context

ContextCheckpoint representa:

> o estado atual de continuidade de uma linha de trabalho.

Um projeto pode possuir múltiplas linhas simultâneas.

Exemplo:

```text id="3se4g8"
project:sofias-memory
    ├── main
    ├── feature:contextual-memory
    ├── release:0.8.0
    └── research:cognee
```

Portanto, o Project Scope isoladamente não identifica um estado de continuidade único.

Foi introduzido:

```text id="wpr9gw"
context_key
```

formando:

```text id="bmreht"
(scope, context_key)
```

denominado **Context Stream**.

Para cada Context Stream existe uma necessidade adicional:

> determinar sem ambiguidade qual checkpoint representa o estado atual.

Essa decisão precisa permanecer correta sob:

- múltiplos callers;
- retries;
- timeout;
- concorrência;
- restart;
- invalidation;
- Forget.

---

## 2. Decision

ContextCheckpoint utilizará:

```text id="2g92d3"
linear revision
+
explicit lifecycle
+
atomic supersession
+
optimistic concurrency
+
database-enforced single current
```

---

## 2.1 Context Stream

A unidade lógica de continuidade é:

```text id="xykxni"
(scope, context_key)
```

`context_key` pertence ao Project Scope.

O mesmo valor pode existir em scopes diferentes.

---

## 2.2 Default stream

Quando o Consumer não especificar contexto especializado:

```text id="bwyp9d"
context_key = main
```

será utilizado.

---

## 2.3 Single ACTIVE invariant

Para um Context Stream:

```text id="7gdsfl"
max ACTIVE checkpoints = 1
```

Essa regra deverá ser garantida também no PostgreSQL através de constraint apropriada.

---

## 2.4 Revision

Cada ContextCheckpoint terá uma revision monotônica dentro de:

```text id="a8ugwi"
(scope, context_key)
```

Exemplo:

```text id="6tgjhg"
1
2
3
4
```

Revision nunca será reutilizada.

---

## 2.5 Full replacement

Atualização de checkpoint não utilizará PATCH semântico.

O Consumer fornecerá o novo estado completo.

Fluxo:

```text id="z6m3vc"
Checkpoint A
    ↓
full replacement
    ↓
Checkpoint B
```

A nova revisão é um novo recurso.

---

## 2.6 Atomic supersession

Quando A é current e B o substitui:

```text id="jrqzek"
A ACTIVE      -> SUPERSEDED
B             -> ACTIVE
```

ambos pertencem à mesma transação.

Não poderá existir estado commitado no qual:

- ambos estejam ACTIVE;
- B esteja ACTIVE sem lineage consistente;
- A tenha sido superseded e B não tenha sido criado.

---

## 2.7 Optimistic stale detection

O Consumer deve identificar explicitamente o checkpoint que acredita ser current.

Conceitualmente:

```text id="ycrm4s"
expected_current_checkpoint_id
```

No Contract v1, o próprio recurso alvo do `/supersede` cumpre essa função.

O serviço compara:

```text id="9383m4"
expected checkpoint
vs
actual ACTIVE checkpoint
```

Se forem diferentes:

```text id="w4nb01"
CONTEXT_STATE_CONFLICT
```

---

## 2.8 PostgreSQL locking

A transação de supersession utilizará mecanismos transacionais PostgreSQL para serializar a alteração do current checkpoint.

A implementação poderá utilizar:

- row-level lock;
- unique constraint;
- combinação dos dois.

Não será introduzido distributed lock externo.

---

## 2.9 No newest-wins

O sistema não determinará current checkpoint através de:

```text id="qx3bfx"
MAX(created_at)
```

ou:

```text id="4pzg7x"
last write wins
```

O current state é uma transição explícita.

---

## 2.10 No automatic rollback

Se um ACTIVE checkpoint for:

- invalidated;
- forgotten;

nenhum checkpoint anterior será automaticamente reativado.

Resultado:

```text id="jmt8ac"
current checkpoint = none
```

até que o Consumer crie um novo current state válido.

---

## 2.11 Idempotent mutation

Cada mutation deverá utilizar `Idempotency-Key`.

Retry da mesma operação lógica deverá reproduzir o resultado original.

Uma retry não poderá criar revision adicional.

---

## 3. Rationale

### 3.1 Continuidade não pode depender do relógio

Timestamp não representa intenção de substituição.

Dois writers podem escrever quase simultaneamente.

---

### 3.2 Concorrência precisa ser visível

Exemplo:

```text id="a865wb"
A = ACTIVE

Consumer 1 reads A
Consumer 2 reads A

Consumer 1:
A -> B

Consumer 2:
A -> C
```

O comportamento seguro é:

```text id="rg1fic"
B = ACTIVE
C = rejected
```

e não:

```text id="3eixxe"
C silently overwrites B
```

---

### 3.3 Full replacement produz snapshots coerentes

Checkpoint representa uma fotografia contextual.

Aplicar patches incrementais poderia gerar estados intermediários ou combinações que o Consumer nunca pretendia produzir.

---

### 3.4 No rollback evita resurrectar verdade obsoleta

Se B foi criado depois de A e B é posteriormente invalidado:

```text id="xya8ue"
B invalid
```

não prova:

```text id="59jl9q"
A is current again
```

O estado correto é ausência de current checkpoint.

---

### 3.5 PostgreSQL já oferece primitivas suficientes

Não há justificativa para Redis locks, ZooKeeper ou outro coordenador distribuído.

---

## 4. Alternatives Considered

### Alternative A — Latest timestamp wins

**Rejected.**

Problemas:

- concorrência silenciosa;
- clock semantics desnecessárias;
- não preserva intenção;
- difícil detectar stale writer.

---

### Alternative B — Mutable checkpoint row

**Rejected.**

Um único registro atualizado in-place perderia:

- histórico;
- lineage;
- comparação;
- auditabilidade.

---

### Alternative C — PATCH incremental

**Rejected para o Contract v1.**

Aumentaria complexidade de:

- Evidence;
- concorrência;
- partial updates;
- retries.

---

### Alternative D — Distributed lock

**Rejected.**

PostgreSQL já é autoridade e possui locking adequado.

---

### Alternative E — Reativar último checkpoint válido automaticamente

**Rejected.**

Checkpoint antigo representa estado histórico, não fallback autoritativo.

---

### Alternative F — Um checkpoint por projeto inteiro

**Rejected.**

Projetos reais podem possuir atividades paralelas.

Por isso existe Context Stream.

---

## 5. Consequences

### Positive

- current state determinístico;
- conflito concorrente explícito;
- histórico preservado;
- revision lineage;
- retries seguros;
- recovery após restart simples;
- nenhum lock externo;
- múltiplos context streams por projeto.

### Negative

- Consumer precisa lidar com `409 CONTEXT_STATE_CONFLICT`;
- novas revisões ocupam mais espaço que atualização in-place;
- Consumers precisam enviar snapshot completo;
- projeto pode ficar temporariamente sem current checkpoint após invalidation/Forget.

### Trade-offs

Aceitamos:

```text id="mlq81b"
explicit conflicts
```

em vez de:

```text id="0za64n"
silent last-write-wins
```

e:

```text id="cq28yz"
temporary absence of current state
```

em vez de:

```text id="ji7cxq"
automatic resurrection of stale context
```

---

## 6. Compatibility / Migration Impact

Esta decisão introduz novos recursos.

Não altera lifecycle de:

- Session;
- MemoryItem;
- Dataset.

Migration deverá criar:

- checkpoint tables;
- revision constraints;
- single-ACTIVE constraint;
- lineage fields/indexes.

Nenhum dado antigo será convertido em checkpoint.

---

## 7. References

- ADR-0012 — First-Class Durable Sessions
- ADR-0015 — Automatic Serialized Migration Bootstrap
- `04-SF-Spec-Memoria-Contextual-Por-Projeto.md`
- `05-SF-Contract-Memoria-Contextual-Por-Projeto.md`
- `06-SF-Architecture-Memoria-Contextual-Por-Projeto.md`

---

# ADR-0019 — Provenance-Aware Evidence and Synchronous Forget Propagation

**Status:** APPROVED\
**Date:** 2026-11-18

---

## 1. Context

Episodes e ContextCheckpoints podem conter conteúdo derivado de outros recursos.

Exemplo:

```text id="esg60t"
SessionEntry A
      ↓
Episode B
      ↓
ContextStatement C
```

Se A for esquecida, simplesmente remover A não é suficiente.

B ou C podem conter paráfrases que continuam revelando o mesmo conteúdo.

Isso criaria:

```text id="gr2f2f"
original content = forgotten
derived copy     = still retrievable
```

e tornaria Forget semanticamente incompleto.

Por outro lado, cascade delete de todos os derivados também seria incorreto.

Exemplo:

```text id="ec7w1m"
Episode X
    Evidence A
    Evidence B
```

Se A for esquecida, B pode continuar sustentando independentemente X.

Portanto, o sistema precisa saber:

```text id="4uls9a"
o que sustenta cada conteúdo derivado
```

e:

```text id="rmbu8w"
o que fazer quando essa sustentação desaparece
```

---

## 2. Decision

Contextual Project Memory introduzirá Evidence first-class e aplicará Forget de maneira:

```text id="zpqf8r"
provenance-aware
deterministic
synchronous
transitive
```

---

## 2.1 Evidence references

Conteúdo contextual derivado deverá referenciar explicitamente suas fontes.

Tipos iniciais:

```text id="se3dsv"
session_entry
memory_item
episode
source
document
chunk
```

A referência é identificada conceitualmente por:

```text id="ke0ygd"
(source_type, source_id)
```

---

## 2.2 Evidence não duplica a fonte

Evidence armazena identidade e disponibilidade.

Ela não deverá manter uma cópia integral do conteúdo original.

---

## 2.3 Support policies

O Contract v1 suporta duas políticas determinísticas:

```text id="5h0veq"
all_required
any_sufficient
```

### all_required

Todas as evidências declaradas são necessárias.

### any_sufficient

Cada evidência declarada é suficiente independentemente.

Enquanto uma permanecer disponível, o conteúdo pode continuar sustentado.

---

## 2.4 Evidence será field-level em checkpoints

ContextCheckpoint contém múltiplas afirmações independentes.

Portanto, Evidence será associada a `ContextStatement`, não somente ao checkpoint inteiro.

Isso permite:

- scrub parcial;
- Forget preciso;
- avaliação independente.

---

## 2.5 Evidence cycles são proibidos

Dependências não poderão formar ciclos.

Exemplo proibido:

```text id="2oxrgp"
Episode A
   ↓
Episode B
   ↓
Episode A
```

A criação deverá falhar.

---

## 2.6 Forget propagation será síncrona

Quando uma Evidence interna for esquecida, a atualização dos derivados contextuais necessários deverá fazer parte da mesma unidade autoritativa de consistência.

Não será aceita uma janela em que:

```text id="7d7q2u"
source forgotten
derived content still retrievable
```

depois que Forget for reportado como concluído.

---

## 2.7 Propagação poderá ser transitiva

Exemplo:

```text id="f89yp8"
Memory A
   ↓
Episode B
   ↓
Episode C
   ↓
Checkpoint Statement D
```

Se Forget de A torna B unsupported e isso torna C unsupported, a propagação continua até estado estável.

---

## 2.8 No LLM evaluation

No Contract v1, um LLM não decidirá:

```text id="cyinlv"
"as evidências restantes parecem suficientes"
```

Suficiência decorre exclusivamente da support policy persistida.

---

## 2.9 Unsupported Episode

Quando um Episode perde suporte necessário:

- não poderá participar do Contextual Recall normal;
- poderá ser invalidado e/ou scrubbed de acordo com a semântica definida pelo domínio;
- conteúdo dependente esquecido não poderá continuar recuperável.

---

## 2.10 Unsupported ContextStatement

Quando um ContextStatement perde suporte:

```text id="m1sdfm"
statement -> unavailable/scrubbed
```

O restante do checkpoint pode permanecer se continuar semanticamente válido.

---

## 2.11 `current_state` é estruturalmente necessário

Um ACTIVE ContextCheckpoint necessita de `current_state` válido.

Se `current_state` perder suporte e não puder mais ser disponibilizado:

```text id="86kgrw"
checkpoint -> INVALIDATED
```

Nenhum checkpoint histórico será reativado automaticamente.

---

## 2.12 Direct Forget também propaga

Episode pode ser Evidence de outro recurso.

Portanto:

```text id="btu9qi"
Forget Episode A
```

deverá atualizar Evidence refs que apontam para A e reavaliar seus dependentes.

---

## 2.13 PostgreSQL é o boundary de consistência

A propagação contextual não dependerá de Neo4j.

Onde a fonte também é PostgreSQL-authoritative, a mutação autoritativa e a atualização contextual deverão convergir transacionalmente para um estado consistente.

---

## 3. Rationale

### 3.1 Forget precisa apagar significado recuperável, não apenas uma row

Se uma paráfrase permanecer recuperável, o Forget original não cumpriu sua intenção.

---

### 3.2 Cascade delete indiscriminado destrói informação legítima

Múltiplas evidências independentes podem sustentar a mesma experiência.

---

### 3.3 Evidence explícita permite comportamento determinístico

Sem provenance estruturada, o serviço teria apenas duas opções ruins:

```text id="234il9"
delete everything
```

ou:

```text id="nrncv9"
guess with AI
```

---

### 3.4 Synchronous propagation remove janela de inconsistência

Um Forget considerado concluído deve ter semântica forte.

O caller não deve precisar aguardar um background worker para saber se o conteúdo ainda pode aparecer.

---

### 3.5 Field-level Evidence melhora precisão

Um checkpoint pode continuar útil mesmo depois que apenas uma de suas afirmações perde suporte.

---

### 3.6 Proibir ciclos simplifica lifecycle

Um DAG de dependências permite propagação previsível.

Ciclos tornariam invalidation/Forget ambíguos e poderiam gerar recursão sem término.

---

## 4. Alternatives Considered

### Alternative A — Cascade delete de todos os derivados

**Rejected.**

Destruiria informação que ainda possui suporte independente.

---

### Alternative B — Não propagar Forget

**Rejected.**

Permitiria resurrection por paráfrase.

---

### Alternative C — Background eventual propagation

**Rejected.**

Criaria janela de inconsistência após Forget concluído.

---

### Alternative D — LLM decide suficiência

**Rejected no Contract v1.**

Problemas:

- não determinístico;
- mais caro;
- difícil auditar;
- comportamento dependente de provider/model;
- inadequado para garantia de Forget.

---

### Alternative E — Evidence somente no checkpoint inteiro

**Rejected.**

Impediria scrub preciso de:

- blocker;
- decision;
- next action;

sem destruir todo o checkpoint.

---

### Alternative F — Duplicar conteúdo original dentro da Evidence

**Rejected.**

Criaria outra cópia que precisaria ser esquecida e aumentaria superfície de vazamento.

---

### Alternative G — Usar Neo4j para dependency traversal

**Rejected no MVP.**

Recursive dependency traversal é viável em PostgreSQL e deve permanecer próxima ao estado autoritativo.

---

## 5. Consequences

### Positive

- Forget forte e consistente;
- provenance rastreável;
- derivação auditável;
- independent evidence preservável;
- comportamento determinístico;
- suporte a scrub parcial;
- nenhuma dependência de IA;
- nenhuma janela eventual pós-Forget;
- propagação transitiva possível.

### Negative

- modelo de Evidence adicional;
- relações adicionais no banco;
- writes de Forget podem se tornar mais caros;
- Consumers precisam declarar support policy corretamente;
- Evidence cycles precisam ser validados;
- integração com domínios de Forget existentes aumenta complexidade transacional.

### Trade-offs

Aceitamos maior complexidade de provenance porque ela protege uma propriedade mais importante:

```text id="quypj6"
Forget realmente significa esquecer
```

Também aceitamos:

```text id="3tb5a1"
Consumer-declared support semantics
```

em vez de:

```text id="j36di9"
AI-inferred support semantics
```

para manter comportamento auditável no MVP.

---

## 6. Compatibility / Migration Impact

Serão necessárias novas estruturas PostgreSQL para:

- Evidence refs;
- Episode-Evidence links;
- Statement-Evidence links;
- availability;
- support modes.

Os workflows de Forget existentes que possam invalidar Evidence deverão integrar-se ao mecanismo contextual.

Isso inclui, quando aplicável:

- Cognitive Forget;
- Knowledge Forget;
- direct Episode Forget.

A mudança não altera o significado público das operações existentes de Forget, mas fortalece sua integração com o novo domínio contextual.

Não haverá dependência de Neo4j para essa propagação.

---

## 7. Operational Impact

Forget poderá executar trabalho adicional proporcional ao número de derivados relacionados.

A implementação deverá:

- usar índices apropriados;
- limitar queries não relacionadas;
- detectar corrupção/ciclos;
- manter transações seguras.

Se benchmarks demonstrarem fan-out excessivo, otimizações poderão ser introduzidas sem enfraquecer a garantia pública de consistência.

---

## 8. References

- ADR-0002 — PostgreSQL Source of Truth and Neo4j Rebuildable Projection
- ADR-0016 — Native Cognitive Memory Model and Lifecycle
- `03-SF-PRD-Memoria-Contextual-Por-Projeto.md`
- `04-SF-Spec-Memoria-Contextual-Por-Projeto.md`
- `05-SF-Contract-Memoria-Contextual-Por-Projeto.md`
- `06-SF-Architecture-Memoria-Contextual-Por-Projeto.md`

---

# Architectural Decision Set Summary

Os três ADRs formam uma sequência coerente.

```text id="n0i4en"
ADR-0017
Contextual Memory becomes first-class
PostgreSQL remains authoritative
        │
        ▼
ADR-0018
ContextCheckpoint gets deterministic
revision + concurrency semantics
        │
        ▼
ADR-0019
Derived contextual memory gets explicit
Evidence + strong Forget semantics
```

O resultado arquitetural é:

```text id="22aqmz"
Sofias Memory
│
├── Knowledge Memory
│
├── Cognitive Memory
│     └── current semantic truth
│
├── Sessions
│     └── temporal context
│
└── Contextual Project Memory
      │
      ├── Episodes
      │     └── historical experience
      │
      ├── Context Streams
      │     └── versioned current continuity
      │
      ├── Evidence
      │     └── provenance-aware support
      │
      └── Contextual Recall
            └── bounded contextual reconstruction
```

As três decisões preservam a fronteira fundamental:

> **Sofias Memory persiste, organiza, recupera, versiona e esquece memória. Ele não interpreta essa memória como autorização, não planeja ações e não executa o trabalho do Consumer.**
