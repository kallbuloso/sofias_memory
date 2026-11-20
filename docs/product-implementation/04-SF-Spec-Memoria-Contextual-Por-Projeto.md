# Sofias Memory — Spec: Memória Contextual Persistente por Projeto

**Document:** `04-SF-Spec-Memoria-Contextual-Por-Projeto.md`\
**Status:** APPROVED\
**Date:** 2026-11-17\
**Project:** Sofias Memory\
**Baseline:**
- `01-SF-Discovery-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `03-SF-PRD-Memoria-Contextual-Por-Projeto.md` — APPROVED

---

## 1. Purpose

Esta Spec define o comportamento esperado da capability de **Memória Contextual Persistente por Projeto** do Sofias Memory.

Ela detalha:

- `Episode`;
- `ContextCheckpoint`;
- Contextual Recall;
- Evidence e provenance contextual;
- lifecycle;
- concorrência;
- correções;
- Forget;
- retomada entre Sessions;
- distinção entre historical experience e current truth;
- invariantes que a implementação deve preservar.

Esta Spec não define ainda:

- schema físico definitivo;
- tabelas;
- migrations;
- endpoints HTTP finais;
- payloads públicos finais;
- nomes definitivos de error codes;
- índices;
- algoritmo matemático de ranking;
- estratégia de embeddings;
- eventual projeção em Neo4j.

Esses elementos pertencem a Contracts, Architecture, ADRs e Technical Backlog.

---

## 2. Scope

Esta Spec cobre o comportamento inicial de memória contextual associada a:

```text
project:<key>
```

Ela define dois novos recursos first-class:

```text
Episode
ContextCheckpoint
```

e uma nova capability comportamental:

```text
Contextual Recall
```

Esses elementos complementam:

```text
Knowledge Memory
Cognitive Memory
Sessions
SessionEntries
```

sem substituir nenhum deles.

---

## 3. Out of Scope

Permanecem fora desta Spec:

- automatic distillation;
- automatic Episode extraction;
- automatic checkpoint generation;
- automatic contradiction resolution;
- automatic semantic supersession;
- agent planning;
- tool execution;
- task management;
- workflow execution;
- provider session management;
- prompt composition;
- multi-user tenancy;
- RBAC;
- ACL por scope;
- MemorySpace first-class;
- Neo4j projection de Episode;
- graph traversal entre Episodes;
- memory decay;
- importance scoring;
- aprendizagem autônoma.

---

# 4. Terminology

## 4.1 Project Scope

Identificador lógico já compatível com Cognitive Memory:

```text
project:<key>
```

Exemplo:

```text
project:sofias-memory
```

Project Scope organiza memória contextual.

Não representa:

- tenant;
- owner;
- user;
- ACL;
- Permission;
- authorization boundary.

---

## 4.2 Context Stream

Um único projeto pode possuir múltiplas atividades independentes em andamento.

Portanto, esta Spec introduz o conceito lógico de:

```text
context_key
```

`context_key` identifica uma linha de continuidade dentro de um Project Scope.

Exemplos:

```text
main
feature:contextual-memory
release:0.8.0
incident:retrieval-001
research:cognee-comparison
```

A identidade lógica de continuidade passa a ser:

```text
(scope, context_key)
```

Exemplo:

```text
scope       = project:sofias-memory
context_key = feature:contextual-memory
```

### 4.2.1 Default Context Stream

Quando o caller não necessitar separar atividades, poderá utilizar conceitualmente:

```text
context_key = main
```

O mecanismo exato de default será definido no Contract.

### 4.2.2 Context Stream não é Task

`context_key` serve apenas para organizar continuidade.

Ele não representa:

- task;
- issue;
- workflow;
- scheduler job;
- permission scope.

---

## 4.3 Episode

Registro persistente de experiência histórica relevante.

Um Episode responde principalmente:

```text
O que aconteceu?
```

e, quando a informação estiver disponível:

```text
Por que aconteceu?
Qual foi o resultado?
Que evidência sustenta esse registro?
```

---

## 4.4 ContextCheckpoint

Registro persistente que representa o estado de continuidade de um Context Stream.

Responde principalmente:

```text
Onde estamos?
```

---

## 4.5 Evidence

Referência persistente para informação que sustenta uma afirmação contextual.

Evidence pode apontar para recursos internos do Sofias Memory ou para uma origem externa descrita por provenance.

---

## 4.6 Current Truth

Informação que representa o estado semântico vigente.

No baseline atual, essa responsabilidade pertence principalmente a:

```text
MemoryItem.semantic
```

Episode não possui automaticamente essa função.

---

## 4.7 Historical Truth

Informação verdadeira sobre um acontecimento passado.

Exemplo:

```text
"Em 10 de outubro foi escolhida a arquitetura A."
```

Essa afirmação pode permanecer historicamente verdadeira mesmo se a arquitetura atual for B.

---

# 5. Core Invariants

As seguintes invariantes MUST ser verdadeiras independentemente da implementação.

---

## INV-01 — Episode não é MemoryItem

`Episode` MUST possuir identidade e lifecycle próprios.

---

## INV-02 — Historical experience não implica current truth

A existência de um Episode MUST NOT tornar seu conteúdo current semantic truth.

---

## INV-03 — MemoryItem não vira Episode automaticamente

A existência de um `MemoryItem` MUST NOT criar Episode implicitamente.

---

## INV-04 — SessionEntry não vira memória automaticamente

`SessionEntry` MUST NOT ser promovida automaticamente para:

- Episode;
- ContextCheckpoint;
- MemoryItem.

---

## INV-05 — Um único current checkpoint

Para cada:

```text
(scope, context_key)
```

MUST existir no máximo um `ContextCheckpoint` com estado `active`.

---

## INV-06 — Current checkpoint é explícito

O current checkpoint MUST ser determinado pelo lifecycle.

Nunca exclusivamente por:

```text
MAX(created_at)
```

ou regra equivalente.

---

## INV-07 — Supersession de checkpoint é atômica

Quando uma nova revisão substitui a current:

```text
old ACTIVE -> SUPERSEDED
new        -> ACTIVE
```

essas mudanças MUST constituir uma única transição consistente.

---

## INV-08 — Stale writer não vence silenciosamente

Um caller operando sobre checkpoint obsoleto MUST NOT sobrescrever silenciosamente estado mais recente.

---

## INV-09 — Forget não pode ser contornado por derivação

Conteúdo esquecido MUST NOT continuar recuperável apenas por existir uma paráfrase derivada em Episode ou ContextCheckpoint.

---

## INV-10 — Memory continua untrusted context

Nenhum conteúdo persistido ou recuperado por esta capability cria autoridade operacional.

---

## INV-11 — PostgreSQL permanece autoridade

O estado necessário para determinar:

- Episode lifecycle;
- ContextCheckpoint lifecycle;
- current checkpoint;
- provenance;
- Forget state;

MUST ser durável e reconstruível a partir do armazenamento autoritativo.

---

## INV-12 — Project scope isolation

Uma operação restrita a um Project Scope MUST NOT incorporar automaticamente conteúdo de outro Project Scope.

---

# 6. Episode Behaviour

## 6.1 Episode Purpose

Episode representa uma experiência que merece sobreviver além da Session na qual ocorreu.

Exemplos:

```text
decision
investigation
experiment
incident
discovery
change
outcome
learning
milestone
```

A enumeração pública final será definida em Contract, mas estes são os tipos comportamentais iniciais esperados.

---

## 6.2 Episode Creation

A criação de Episode no MVP MUST ser explícita.

Fluxo conceitual:

```text
caller decides experience is relevant
                ↓
constructs Episode
                ↓
provides project scope
                ↓
provides historical content
                ↓
provides provenance/evidence when applicable
                ↓
Sofias Memory validates
                ↓
Episode persists
```

O Sofias Memory MUST NOT analisar uma Session autonomamente e concluir:

```text
"isto merece virar Episode"
```

no escopo inicial.

---

## 6.3 Minimum Semantic Content

Todo Episode não esquecido MUST possuir semanticamente:

```text
identity
scope
kind
summary
occurred_at
lifecycle
provenance
```

Pode possuir também:

```text
context_key
situation
reason
outcome
evidence
related resources
```

Os nomes finais dos campos serão definidos no Contract.

---

## 6.4 `occurred_at`

Episode representa acontecimento histórico.

Portanto, deve existir uma referência temporal do acontecimento distinta de:

```text
created_at
```

Exemplo:

```text
occurred_at = quando a decisão aconteceu
created_at  = quando o Episode foi persistido
```

Esses valores podem ser diferentes.

---

## 6.5 Episode Immutability

Depois de criado, o conteúdo semântico de um Episode SHOULD ser imutável.

Correção de significado não deve ocorrer através de edição silenciosa.

Caso um Episode seja posteriormente considerado incorreto, deverá ser utilizado seu lifecycle de invalidação.

Exceções permitidas:

- scrub causado por Forget;
- atualização de disponibilidade de Evidence;
- metadata operacional não semântica explicitamente autorizada pelo futuro Contract.

---

# 7. Episode Lifecycle

O lifecycle inicial de Episode será:

```text
active
invalidated
forgotten
```

---

## 7.1 ACTIVE

Significa:

> o Episode está disponível como registro histórico recuperável.

`active` NÃO significa:

> essa informação é current truth.

---

## 7.2 INVALIDATED

Significa:

> o Episode foi preservado como registro estrutural/auditável, mas não deve participar de recall normal como experiência confiável.

Pode ocorrer quando:

- o caller declara o Episode incorreto;
- a evidência necessária deixa de sustentar o conteúdo;
- uma inconsistência estrutural torna o conteúdo não confiável.

Um Episode `invalidated` MAY permanecer disponível em operações administrativas ou históricas explícitas.

Ele MUST ser excluído do recall contextual normal.

---

## 7.3 FORGOTTEN

Significa:

> o conteúdo recuperável do Episode foi removido de acordo com Forget semantics.

O recurso MAY manter tombstone mínimo necessário para:

- identidade;
- lifecycle;
- idempotência;
- auditabilidade estrutural.

O conteúdo esquecido MUST NOT ser retornado por Contextual Recall.

---

## 7.4 No Episode Supersession

Episode não utiliza `superseded` como mecanismo normal de evolução.

Motivo:

```text
Episode A:
"Em março decidimos A."

Episode B:
"Em abril abandonamos A e decidimos B."
```

B não torna A historicamente falso.

Portanto:

```text
new Episode != supersede old Episode
```

---

# 8. Episode Correction

Quando um Episode estiver factual ou semanticamente incorreto:

```text
Episode incorrect
       ↓
invalidate
       ↓
optional corrected Episode
```

A correção SHOULD preservar relação auditável entre:

```text
invalidated Episode
corrected Episode
```

sem modificar retroativamente o acontecimento original de maneira silenciosa.

---

# 9. Evidence Behaviour

## 9.1 Evidence Purpose

Evidence existe para responder:

```text
De onde veio esta informação?
```

e também para permitir comportamento correto quando a fonte deixa de estar disponível.

---

## 9.2 Internal Evidence

Pode referenciar recursos como:

```text
Session
SessionEntry
MemoryItem
Episode
Knowledge Source
Document
Chunk
Query/provenance record
```

quando suportado pelo futuro Contract.

---

## 9.3 External Evidence

Quando a origem não for recurso interno do Sofias Memory, provenance deverá permitir identificar a origem externa sem exigir que o Memory se torne owner desse recurso.

---

## 9.4 Evidence Availability

Cada Evidence deverá possuir conceitualmente um estado de disponibilidade:

```text
available
unavailable
forgotten
```

ou semântica equivalente.

A forma física será definida posteriormente.

---

# 10. Evidence Support Semantics

O Sofias Memory MUST NOT utilizar um LLM para decidir automaticamente, no MVP:

> as evidências restantes ainda são suficientes?

Essa decisão precisa ser determinística.

Portanto, para conteúdo derivado, o caller deverá declarar a relação de dependência entre afirmação e Evidence.

---

## 10.1 Support Policy

Conceitualmente, uma afirmação derivada poderá declarar:

```text
all_required
```

ou:

```text
any_sufficient
```

### `all_required`

Todas as evidências declaradas são necessárias.

Se uma ficar indisponível:

```text
derived assertion becomes unsupported
```

### `any_sufficient`

Cada evidência declarada é considerada suficiente independentemente.

Enquanto uma permanecer disponível:

```text
derived assertion may remain supported
```

A representação pública final será definida no Contract.

---

## 10.2 Caller Responsibility

No MVP, é responsabilidade do caller declarar corretamente a política de suporte.

Sofias Memory valida e aplica a política.

Ele não inventa a política através de inferência.

---

# 11. Field-Level Evidence for Checkpoints

ContextCheckpoint pode conter múltiplas afirmações semanticamente independentes.

Exemplo:

```text
objective
current_state
approved_decisions[]
pending[]
blockers[]
next_action
```

Uma única Evidence global para o checkpoint inteiro seria insuficiente para Forget preciso.

Portanto, cada unidade semântica derivada SHOULD ser capaz de possuir sua própria relação com Evidence.

Exemplo conceitual:

```text
objective
  evidence: [E1]

decision A
  evidence: [E2, E3]
  support: any_sufficient

blocker B
  evidence: [E4]

next_action
  evidence: [E5]
```

A estrutura física não é definida nesta Spec.

---

# 12. ContextCheckpoint Behaviour

## 12.1 Purpose

ContextCheckpoint representa continuidade de trabalho.

Ele é uma fotografia semântica compacta, não uma transcrição.

---

## 12.2 Logical Identity

Todo checkpoint pertence a:

```text
(scope, context_key)
```

Exemplo:

```text
project:sofias-memory
feature:contextual-memory
```

---

## 12.3 Content

ContextCheckpoint MUST ser capaz de representar:

```text
summary/current_state
```

e MAY representar:

```text
objective
approved_decisions[]
pending[]
open_items[]
blockers[]
next_action
evidence
```

O futuro Contract definirá quais campos são obrigatórios.

---

## 12.4 Checkpoint is not Session Summary

Checkpoint não deve ser definido como:

```text
summary of last Session
```

Ele representa o estado de continuidade do Context Stream.

Pode ser produzido após:

- uma Session;
- várias Sessions;
- execução de agente;
- operação administrativa;
- decisão explícita do caller.

---

# 13. ContextCheckpoint Lifecycle

O lifecycle inicial será:

```text
active
superseded
invalidated
forgotten
```

---

## 13.1 ACTIVE

É o checkpoint current do:

```text
(scope, context_key)
```

Pode existir no máximo um.

---

## 13.2 SUPERSEDED

Representa checkpoint que foi current anteriormente e foi substituído por uma nova revisão válida.

Pode participar de histórico explícito.

Não participa como current state.

---

## 13.3 INVALIDATED

Representa checkpoint que não deve mais ser considerado confiável.

Pode ocorrer por:

- perda de evidência essencial;
- inconsistência detectada;
- invalidação explícita do caller.

Se o checkpoint invalidado era ACTIVE:

```text
nenhum checkpoint anterior volta automaticamente a ser ACTIVE
```

Essa regra evita resurrectar estado antigo.

---

## 13.4 FORGOTTEN

O conteúdo recuperável foi scrubbed de acordo com Forget.

Se um checkpoint ACTIVE for esquecido:

```text
current checkpoint becomes absent
```

Nenhum checkpoint superseded retorna automaticamente para ACTIVE.

---

# 14. Checkpoint Revision Behaviour

## 14.1 Create Initial Checkpoint

Se não existir checkpoint ACTIVE para:

```text
(scope, context_key)
```

o caller pode criar o primeiro checkpoint.

Resultado:

```text
new checkpoint = ACTIVE
```

---

## 14.2 Replace Current Checkpoint

Quando existir checkpoint ACTIVE, a criação de nova revisão deverá declarar conceitualmente:

```text
expected_current_checkpoint
```

A operação válida realiza:

```text
expected checkpoint == actual ACTIVE
                    ↓
old -> SUPERSEDED
new -> ACTIVE
```

---

## 14.3 Stale Revision

Caso:

```text
expected != actual ACTIVE
```

a operação falha.

Resultado:

```text
no new ACTIVE checkpoint
no mutation of current state
```

O caller deve:

```text
re-read current checkpoint
reconcile its intended update
retry as a new operation
```

---

## 14.4 No Blind Retry

Conflict de estado não deve ser resolvido apenas trocando idempotency key e reenviando o mesmo conteúdo.

---

# 15. Checkpoint Idempotency Behaviour

A mesma operação lógica repetida devido a timeout ou retry MUST NOT produzir múltiplas revisões equivalentes.

A estratégia exata será definida no Contract, mas o comportamento obrigatório é:

```text
same operation + same payload
    -> same logical result
```

e:

```text
same operation identity + different payload
    -> conflict
```

---

# 16. Contextual Recall

## 16.1 Purpose

Contextual Recall reconstrói Project Context utilizando fontes apropriadas à intenção da consulta.

Ele não substitui:

```text
Knowledge Recall
Cognitive Recall
Session APIs
```

---

## 16.2 Output Nature

O resultado MUST ser contexto estruturado.

O serviço não precisa produzir uma resposta natural final.

Exemplo conceitual:

```text
ProjectContextBundle
  current_checkpoint
  current_memories[]
  episodes[]
  evidence[]
  knowledge_references[]
  warnings[]
```

O payload final pertence ao Contract.

---

# 17. Contextual Recall Intents

Para impedir que Contextual Recall se transforme em:

```text
"buscar tudo em todos os lugares"
```

a capability deverá diferenciar pelo menos quatro intenções comportamentais.

Os nomes públicos finais podem mudar no Contract.

---

## 17.1 RESUME

Perguntas típicas:

```text
Onde paramos?
O que falta?
Qual é o próximo passo?
Continue de onde paramos.
```

Prioridade comportamental:

```text
1. current ContextCheckpoint
2. current Cognitive Memory relevante
3. recent/relevant Episodes
4. supporting Evidence quando necessário
```

Se não existir checkpoint current, o sistema MUST sinalizar sua ausência.

Não deve fabricar continuidade.

---

## 17.2 EXPLAIN

Perguntas típicas:

```text
Por que escolhemos isso?
Como chegamos a essa decisão?
O que motivou essa mudança?
```

Prioridade comportamental:

```text
1. current semantic state quando relevante
2. related Episodes
3. Evidence
4. supporting Knowledge
```

---

## 17.3 HISTORY

Perguntas típicas:

```text
O que decidimos antes?
Quando mudamos essa arquitetura?
Como essa ideia evoluiu?
```

Prioridade comportamental:

```text
1. Episodes
2. historical checkpoints
3. relevant semantic history
4. Evidence
```

Resultados históricos MUST permanecer claramente identificados como históricos.

---

## 17.4 CONTEXT

Consulta geral sobre o projeto.

Pode combinar:

```text
current checkpoint
current semantic memory
episodes
knowledge
```

de maneira seletiva.

---

# 18. Contextual Recall Scope Rules

## 18.1 Primary Project Scope

Uma operação contextual MVP deverá operar sobre um Project Scope explícito.

Exemplo:

```text
project:sofias-memory
```

---

## 18.2 Context Stream

A consulta MAY também especificar:

```text
context_key
```

Quando especificado, o current checkpoint deve ser resolvido dentro desse stream.

---

## 18.3 No Implicit Cross-Project Recall

Contextual Recall MUST NOT buscar automaticamente outros project scopes.

---

## 18.4 Global Cognitive Memory

Global Cognitive Memory não será automaticamente incorporada ao Project Context no MVP.

Um consumer poderá recuperá-la separadamente conforme seus próprios contratos.

A incorporação automática de `global` em Contextual Recall fica DEFERRED.

Motivo:

> manter o primeiro contrato de Project Context explícito e impedir mistura silenciosa de scopes.

---

# 19. Contextual Recall Selection Rules

## 19.1 Current State Queries

Quando a intenção depender de estado atual:

```text
current ContextCheckpoint
current Cognitive Memory
```

devem prevalecer sobre material histórico conflitante.

---

## 19.2 Historical Queries

Quando a consulta for histórica:

```text
Episode
historical checkpoint
```

podem ser retornados mesmo quando não representam estado atual.

---

## 19.3 Causal Queries

Perguntas causais devem privilegiar Episodes e suas evidências.

---

## 19.4 Knowledge Expansion

Knowledge Memory deve ser incorporada quando:

- for explicitamente solicitada;
- servir como Evidence;
- a consulta depender de documentação source-backed.

Não precisa ser consultada para toda operação contextual.

---

## 19.5 Session Expansion

SessionEntries não devem ser despejadas integralmente no resultado.

Quando necessárias como Evidence:

```text
only relevant referenced entries
```

devem ser recuperadas.

---

# 20. Bounded Context

Contextual Recall MUST possuir resultado limitado.

O sistema não poderá utilizar como estratégia válida:

```text
return entire Project Memory
```

O algoritmo de budgeting será definido posteriormente, mas deve ser possível limitar:

- quantidade de itens;
- volume textual;
- expansão de Evidence.

---

# 21. Contradictions

No MVP, Contextual Recall MUST NOT resolver automaticamente contradições através de IA.

Exemplo:

```text
MemoryItem current:
"A arquitetura atual é B."

Episode historical:
"Foi escolhida a arquitetura A."
```

O comportamento correto é preservar as semânticas:

```text
current truth = B
historical event = A was once chosen
```

não apagar ou fundir automaticamente uma delas.

---

# 22. Ambiguous Current Truth

Se houver inconsistência de dados que impeça determinar current truth de forma segura:

```text
Contextual Recall MUST expose ambiguity
```

em vez de escolher silenciosamente um item.

A forma pública do warning será definida no Contract.

---

# 23. Missing Context

Ausência de memória relevante não constitui necessariamente erro.

Exemplo:

```text
RESUME
```

sem ContextCheckpoint current.

Resultado conceitual:

```text
current_checkpoint = none
continuity_available = false
```

O serviço MUST NOT inventar:

```text
next_action
objective
current_state
```

---

# 24. Direct Forget

Episodes e ContextCheckpoints são memória persistente first-class.

Portanto, ambos deverão suportar Forget explícito em seus futuros contratos.

---

## 24.1 Episode Forget

Forget direto de Episode:

```text
ACTIVE or INVALIDATED
        ↓
FORGOTTEN
```

Conteúdo recuperável deve ser scrubbed.

---

## 24.2 ContextCheckpoint Forget

Forget direto de checkpoint:

```text
ACTIVE | SUPERSEDED | INVALIDATED
                    ↓
FORGOTTEN
```

Se era ACTIVE:

```text
no automatic fallback
```

para checkpoint anterior.

---

# 25. Forget Propagation

Quando outro recurso Sofias Memory sofre Forget e é Evidence para conteúdo contextual:

```text
Evidence becomes unavailable
          ↓
dependent assertions evaluated
```

---

## 25.1 `all_required`

Se uma evidência necessária for perdida:

```text
assertion unsupported
```

A informação correspondente MUST deixar de ser recuperável.

---

## 25.2 `any_sufficient`

Se houver outra Evidence disponível:

```text
assertion remains supported
```

A provenance deve refletir a mudança.

---

# 26. Partial Checkpoint Invalidation

Como checkpoints podem possuir várias afirmações independentes, perda de Evidence não precisa invalidar o checkpoint inteiro.

Exemplo:

```text
objective         -> still supported
current_state     -> still supported
decision A        -> still supported
blocker B         -> unsupported
next_action       -> unsupported
```

O sistema poderá scrubbar/inativar apenas as unidades sem suporte.

Entretanto, após scrub, deverá avaliar se o checkpoint ainda representa continuidade suficiente.

Se deixar de representar um estado coerente:

```text
checkpoint -> INVALIDATED
```

A política exata de suficiência estrutural será definida no Contract/Architecture, mas MUST ser determinística no MVP.

---

# 27. No AI-based Forget Decision

No MVP, nenhum LLM poderá decidir:

```text
"essa informação provavelmente ainda pode ficar"
```

após Forget.

A decisão deverá decorrer de:

- Evidence;
- support policy;
- regras determinísticas.

---

# 28. Provenance Safety

Quando Forget atingir Evidence:

- conteúdo esquecido não poderá permanecer em logs sem necessidade;
- conteúdo esquecido não poderá permanecer em debug payloads;
- conteúdo esquecido não poderá permanecer em caches persistentes;
- provenance não poderá manter external references que violem a semântica de Forget aplicável.

A política final de tombstone será definida no Contract correspondente.

---

# 29. Session Behaviour

Sessions continuam possuindo sua semântica existente.

Esta feature não altera:

```text
Session lifecycle
SessionEntry append-only behaviour
```

Episode e Checkpoint podem referenciar Sessions como Evidence, mas:

```text
Session != Project
Session != Episode
Session != ContextCheckpoint
```

---

# 30. Session Archive

Archive de Session não equivale a Forget.

Portanto:

```text
archived Session evidence
```

pode continuar disponível para Episode/Checkpoint se seu conteúdo continuar existente e seu contrato de leitura permitir.

---

# 31. Session Forget Boundary

O baseline atual estabelece que Cognitive Forget não implica automaticamente apagar Conversation history externa ou Session history.

Esta feature MUST respeitar essa separação.

Entretanto:

> um recurso derivado não pode manter uma cópia recuperável de conteúdo que seu próprio domínio foi obrigado a esquecer.

---

# 32. Cognitive Memory Behaviour

Cognitive Memory permanece:

```text
profile
semantic
```

---

## 32.1 Current Truth

Para perguntas de estado atual, Cognitive Memory current SHOULD ser preferida a Episode histórico quando ambos se referirem ao mesmo conceito.

---

## 32.2 Historical Cognitive Memory

Quando `as_of` ou histórico cognitivo forem explicitamente utilizados, esses resultados devem continuar semanticamente distintos de Episode.

---

## 32.3 Episode + MemoryItem Relationship

Um caller MAY registrar:

```text
Episode:
"Após análise, decidimos utilizar PostgreSQL."

MemoryItem.semantic:
"PostgreSQL é o banco autoritativo do projeto."
```

Esses registros têm funções distintas.

---

# 33. Knowledge Memory Behaviour

Knowledge Memory continua sendo o domínio para:

- documentos;
- URLs;
- arquivos;
- source-backed knowledge;
- chunks;
- entities;
- relations.

Episode não substitui Knowledge Memory.

Um Episode pode referenciar:

```text
Document
Chunk
Source
```

como Evidence.

---

# 34. Failure Semantics

## 34.1 Validation Failure

Deve ocorrer quando:

- scope for inválido;
- context_key for inválido;
- kind não suportado;
- conteúdo obrigatório ausente;
- Evidence reference interna for estruturalmente inválida;
- support policy for inválida.

Nenhuma mutação parcial deve ocorrer.

---

## 34.2 Not Found

Recuperar recurso inexistente deve produzir resultado distinguível de recurso FORGOTTEN quando o Contract exigir essa distinção.

---

## 34.3 Stale Checkpoint Conflict

Tentativa de substituir checkpoint não current deve resultar em conflict.

Nenhuma mutação parcial.

---

## 34.4 Idempotency Conflict

Mesma identidade de operação com payload diferente deve produzir conflict.

---

## 34.5 Evidence Unavailable

Criação de conteúdo derivado cuja Evidence obrigatória já esteja indisponível deve falhar.

---

## 34.6 Recall Dependency Failure

Se uma fonte opcional de Contextual Recall estiver temporariamente indisponível:

- o resultado poderá degradar parcialmente;
- a degradação deverá ser explicitamente observável;
- informação não recuperada não poderá ser inventada.

O comportamento exato dependerá da arquitetura das fontes envolvidas.

---

# 35. Edge Cases

## EC-01 — Primeiro checkpoint

Nenhum checkpoint existe.

Caller cria um.

Resultado:

```text
ACTIVE
```

---

## EC-02 — Dois writers concorrentes

Ambos leem checkpoint A.

Writer 1 cria B.

Writer 2 tenta criar C com expectativa A.

Resultado:

```text
B remains ACTIVE
C rejected
```

---

## EC-03 — Retry após timeout

Caller não sabe se criação de B concluiu.

Repete mesma operação lógica.

Resultado:

```text
same logical B returned
```

não:

```text
new C created
```

---

## EC-04 — Episode antigo contradiz estado atual

Episode:

```text
"Escolhemos A."
```

Current MemoryItem:

```text
"Usamos B."
```

Resultado:

```text
A = historical
B = current
```

Sem auto-merge.

---

## EC-05 — Active checkpoint forgotten

Resultado:

```text
no active checkpoint
```

Checkpoint superseded anterior não volta a ser current.

---

## EC-06 — Active checkpoint invalidated

Mesmo comportamento:

```text
no automatic rollback
```

---

## EC-07 — Evidence única esquecida

Episode derivado deixa de expor conteúdo suportado exclusivamente por ela.

---

## EC-08 — Uma de várias evidências independentes esquecida

Episode pode permanecer disponível se policy declarar as demais suficientes.

---

## EC-09 — Session arquivada

Episode que referencia Session arquivada não é automaticamente invalidado.

---

## EC-10 — Project sem memória contextual

Contextual Recall retorna conjunto vazio/continuity unavailable.

Não é erro de domínio.

---

## EC-11 — Vários context streams

Projeto:

```text
project:sofias-memory
```

pode ter simultaneamente:

```text
main
feature:contextual-memory
release:0.8.0
```

Cada um pode possuir seu próprio ACTIVE checkpoint.

---

# 36. Context Stream Invariants

## CST-01

`context_key` é local ao scope.

Portanto:

```text
(project:a, main)
```

e:

```text
(project:b, main)
```

são contextos diferentes.

---

## CST-02

Single-active aplica-se a:

```text
(scope, context_key)
```

não ao sistema inteiro.

---

## CST-03

Episodes podem existir sem `context_key` quando forem relevantes ao projeto inteiro.

---

## CST-04

ContextCheckpoint sempre pertence a um Context Stream.

---

# 37. Observability

O sistema SHOULD tornar observáveis:

- criação de Episode;
- invalidação;
- Forget;
- criação de checkpoint;
- supersession;
- stale conflicts;
- Contextual Recall;
- fontes consultadas;
- fontes degradadas;
- quantidade de itens selecionados.

Observability MUST NOT registrar conteúdo sensível além do necessário.

---

# 38. Auditability

Deverá ser possível explicar, quando aplicável:

```text
por que este checkpoint é current?
qual checkpoint ele substituiu?
de onde veio esta decisão?
quais evidências sustentam este Episode?
por que este item não está mais disponível?
```

sem depender exclusivamente de logs externos.

---

# 39. Compatibility Behaviour

Consumidores existentes que desconhecem:

```text
Episode
ContextCheckpoint
Contextual Recall
```

devem continuar funcionando.

A capability nova deverá ser detectável através do mecanismo explícito de compatibility/capability negotiation já adotado pelo Sofias Memory.

Nenhum consumidor deve inferir suporte somente através de:

```text
application_version >= X
```

---

# 40. Minimum Behavioural Experiment

A implementação deverá provar o seguinte cenário.

---

## Step 1 — Create Context

Criar:

```text
scope       = project:test
context_key = feature:memory
```

---

## Step 2 — Session A

Registrar contexto temporal.

Criar:

```text
MemoryItem.semantic
Episode
ContextCheckpoint A
```

Checkpoint A:

```text
objective:
  implementar memory feature

current_state:
  design aprovado

pending:
  implementar persistence

next_action:
  criar migration
```

---

## Step 3 — Restart

Encerrar:

- consumer;
- Sofias Memory process.

Reiniciar ambos.

---

## Step 4 — Session B

Executar Contextual Recall com intenção:

```text
RESUME
```

O resultado deve permitir determinar:

```text
objective
current_state
pending
next_action
```

sem replay integral de Session A.

---

## Step 5 — Explain

Consulta:

```text
Por que escolhemos esta arquitetura?
```

Contextual Recall `EXPLAIN` deve recuperar:

```text
current semantic state
relevant Episode
Evidence
```

---

## Step 6 — Replace Checkpoint

Criar B sobre A.

Resultado:

```text
A -> SUPERSEDED
B -> ACTIVE
```

---

## Step 7 — Stale Conflict

Tentar criar C declarando A como current esperado.

Resultado:

```text
conflict
B remains ACTIVE
```

---

## Step 8 — Historical Recall

Consulta `HISTORY`.

A deve permanecer historicamente disponível enquanto não invalidado/esquecido.

---

## Step 9 — Forget Evidence

Esquecer Evidence exclusiva de determinada afirmação do Episode.

Resultado:

```text
derived content no longer recoverable
```

---

## Step 10 — Scope Isolation

Criar:

```text
project:other
```

Contextual Recall em `project:test` não pode incorporar automaticamente conteúdo de `project:other`.

---

# 41. Acceptance Criteria

## AC-SPEC-01

Episode pode ser persistido explicitamente com historical semantics.

## AC-SPEC-02

Episode não altera current truth.

## AC-SPEC-03

Episode pode ser invalidado sem apagar a existência histórica estrutural do registro.

## AC-SPEC-04

Episode pode ser esquecido.

## AC-SPEC-05

ContextCheckpoint pode ser persistido para `(scope, context_key)`.

## AC-SPEC-06

No máximo um checkpoint fica ACTIVE por `(scope, context_key)`.

## AC-SPEC-07

Supersession de checkpoint é atômica.

## AC-SPEC-08

Writer stale não sobrescreve current checkpoint.

## AC-SPEC-09

Retry idempotente não produz revisão duplicada.

## AC-SPEC-10

Checkpoint ACTIVE esquecido não reativa checkpoint anterior.

## AC-SPEC-11

Checkpoint ACTIVE invalidado não reativa checkpoint anterior.

## AC-SPEC-12

Contextual Recall `RESUME` favorece current checkpoint.

## AC-SPEC-13

Contextual Recall `EXPLAIN` consegue incluir Episodes e Evidence.

## AC-SPEC-14

Contextual Recall `HISTORY` distingue conteúdo histórico de current truth.

## AC-SPEC-15

Contextual Recall retorna contexto estruturado sem exigir resposta LLM final.

## AC-SPEC-16

Ausência de contexto não gera conteúdo inventado.

## AC-SPEC-17

Recall não mistura automaticamente project scopes.

## AC-SPEC-18

SessionEntry não é promovida automaticamente.

## AC-SPEC-19

Cognitive Memory continua `profile|semantic`.

## AC-SPEC-20

Evidence dependency é determinística no MVP.

## AC-SPEC-21

Forget de Evidence exclusiva remove disponibilidade do conteúdo derivado.

## AC-SPEC-22

Forget de uma Evidence em `any_sufficient` pode preservar derivado sustentado pelas restantes.

## AC-SPEC-23

Forget não utiliza decisão por LLM.

## AC-SPEC-24

Episodes e Checkpoints sobrevivem a restart enquanto disponíveis.

## AC-SPEC-25

Nova capability não altera semântica dos contratos existentes.

---

# 42. Deferred Behaviour

Permanecem explicitamente DEFERRED:

- criação automática de Episode;
- criação automática de ContextCheckpoint;
- Session distillation;
- AI conflict resolution;
- AI evidence sufficiency;
- global memory auto-injection;
- cross-project recall;
- automatic context stream discovery;
- graph projection;
- relation graph entre Episodes;
- importance;
- decay;
- automatic checkpoint compaction;
- automatic rollback para checkpoint histórico;
- automatic Task synchronization;
- autonomous memory writes.

---

# 43. Normative Summary

A capability deverá respeitar o seguinte modelo:

```text
Project Scope
   │
   ├── Cognitive Memory
   │      └── what is currently known
   │
   ├── Sessions
   │      └── temporal interaction history
   │
   ├── Episodes
   │      └── what happened and why
   │
   └── Context Streams
          └── ContextCheckpoint
                 └── where this work currently stands
```

Contextual Recall deverá reconstruir somente o Project Context necessário para a intenção corrente.

O modelo deverá manter separadas três perguntas fundamentais:

```text
What is true now?
    -> Cognitive Memory

What happened?
    -> Episode

Where did we stop?
    -> ContextCheckpoint
```

e preservar Evidence suficiente para responder:

```text
Why do we believe this?
```

sem transformar Sofias Memory em agente, planner, task manager ou autoridade operacional.
