# Sofias Memory — PRD: Memória Contextual Persistente por Projeto

**Document:** `03-SF-PRD-Memoria-Contextual-Por-Projeto.md`\
**Status:** APPROVED\
**Date:** 2026-11-17\
**Project:** Sofias Memory\
**Baseline:**
- `01-SF-Discovery-Memoria-Contextual-Por-Projeto.md` — APPROVED
- `02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md` — APPROVED

---

## 1. Overview

O Sofias Memory deverá oferecer uma capability de **memória contextual persistente por projeto** capaz de preservar e recuperar continuidade entre diferentes Sessions, conversas, processos e reinicializações.

A capability deverá complementar os domínios já existentes:

- Knowledge Memory;
- Native Cognitive Memory;
- Sessions;
- SessionEntries;
- Skills;
- Agent Profiles.

A evolução deverá permitir que consumidores recuperem não apenas fatos persistentes, mas também:

- experiências históricas relevantes;
- justificativas de decisões;
- mudanças de direção;
- estado atual de continuidade;
- pendências;
- próximo ponto de retomada.

O produto deverá distinguir explicitamente:

```text
what is known
what happened
where we are
why we got here
```

sem transformar esses conceitos em uma única estrutura genérica.

---

## 2. Product Goals

### G-01 — Continuidade entre Sessions

Permitir que uma nova Session recupere o contexto necessário para continuar trabalho realizado anteriormente sem depender do replay integral das Sessions anteriores.

---

### G-02 — Preservação de experiências

Permitir o armazenamento persistente de experiências históricas relevantes através de `Episode`.

---

### G-03 — Estado de continuidade

Permitir o armazenamento de um `ContextCheckpoint` que represente o estado atual de um contexto duradouro.

---

### G-04 — Distinção entre verdade atual e história

Garantir que:

```text
MemoryItem.semantic
```

represente semantic/current truth, enquanto:

```text
Episode
```

represente experiência histórica.

---

### G-05 — Recuperação contextual composta

Permitir recuperação coordenada entre diferentes planos de memória quando necessário para reconstruir Project Context.

---

### G-06 — Proveniência verificável

Garantir que Episodes e ContextCheckpoints derivados de outras informações mantenham referências suficientes para explicar sua origem.

---

### G-07 — Forget consistente

Garantir que informações esquecidas não permaneçam recuperáveis por meio de derivados contextuais não sustentados por evidência independente.

---

### G-08 — Compatibilidade

Adicionar a capability sem alterar silenciosamente o comportamento das APIs e contratos existentes.

---

## 3. Non-Goals

A capability não deverá transformar o Sofias Memory em:

- Agent Runtime;
- Task Manager;
- Project Manager;
- Workflow Engine;
- Scheduler;
- Tool Executor;
- Prompt Framework;
- LLM Gateway;
- Authorization Engine;
- Conversation UI;
- autonomous planner;
- generic multi-user memory platform.

Também não faz parte desta entrega inicial:

- automatic session distillation;
- automatic episode extraction;
- automatic memory promotion;
- autonomous contradiction resolution;
- autonomous supersession;
- graph projection de Episodes;
- MemorySpace first-class;
- generic tenancy;
- RBAC;
- ACL por scope;
- importance scoring sem política definida.

---

## 4. Target Consumers

### 4.1 Conversational Assistants

Assistentes que trabalham continuamente sobre projetos, pesquisas ou atividades de longa duração.

O principal problema resolvido é:

> iniciar nova conversa sem depender de recapitulação manual do usuário.

---

### 4.2 Agents

Agentes externos poderão persistir experiências relevantes resultantes de:

- investigações;
- execuções;
- diagnósticos;
- experimentos;
- pesquisas;
- incidentes.

O Sofias Memory não executará esses agentes.

---

### 4.3 Applications

Aplicações poderão utilizar a capability para preservar contexto relacionado a:

- projetos;
- casos;
- pesquisas;
- processos;
- atividades persistentes.

---

### 4.4 Automations

Automations independentes poderão utilizar o mesmo contexto lógico para compartilhar memória persistente entre execuções.

---

## 5. Domain Concepts

---

## 5.1 Project Memory

Project Memory representa logicamente todo o conteúdo persistente relacionado a um mesmo projeto.

Pode incluir:

- Cognitive Memory;
- Knowledge Memory;
- Sessions;
- Episodes;
- ContextCheckpoints.

Project Memory não exige, neste escopo, uma entidade física `Project` ou `MemorySpace`.

---

## 5.2 Project Context

Project Context representa somente a seleção necessária para compreender ou continuar a situação corrente.

Project Memory pode crescer indefinidamente.

Project Context deve permanecer seletivo.

---

## 5.3 Episode

`Episode` é um registro persistente de experiência histórica relevante.

Exemplos:

- decisão;
- investigação;
- experimento;
- incidente;
- descoberta;
- resultado;
- mudança de direção;
- aprendizado;
- marco relevante.

Episode MUST NOT ser tratado automaticamente como verdade vigente.

---

## 5.4 ContextCheckpoint

`ContextCheckpoint` representa uma fotografia semântica do estado de continuidade de um contexto duradouro.

Pode representar:

- objective;
- current state;
- approved decisions;
- open items;
- blockers;
- pending work;
- next action;
- relevant evidence.

ContextCheckpoint MUST NOT ser tratado como autoridade operacional sobre tasks externas.

---

## 5.5 Contextual Recall

Contextual Recall representa a recuperação coordenada de diferentes tipos de evidência necessária para reconstruir Project Context.

Pode utilizar:

- Cognitive Memory;
- Episodes;
- ContextCheckpoint;
- Session evidence;
- Knowledge Memory.

---

## 5.6 Evidence

Evidence é uma referência persistente para informação que sustenta um Episode ou ContextCheckpoint.

Evidence pode apontar para recursos já existentes ou futuros do Sofias Memory.

A existência de uma Evidence reference não implica que seu conteúdo esteja permanentemente disponível.

---

## 6. Scope Model

### FR-SCOPE-01

A capability MUST suportar inicialmente contexto baseado no formato já existente:

```text
project:<key>
```

---

### FR-SCOPE-02

O matching de project scope MUST permanecer explícito.

---

### FR-SCOPE-03

Memória contextual pertencente a:

```text
project:a
```

MUST NOT ser automaticamente recuperada em consultas restritas a:

```text
project:b
```

---

### FR-SCOPE-04

`project:<key>` MUST NOT ser tratado como:

- tenant;
- user;
- owner;
- authorization boundary;
- ACL.

---

### FR-SCOPE-05

A futura introdução de `MemorySpace` não faz parte deste PRD.

---

## 7. Episode Requirements

### FR-EP-01 — First-class resource

O produto MUST possuir um recurso first-class para Episode.

Episode MUST NOT ser representado como:

- `MemoryItem`;
- `SessionEntry`;
- fake `Document`;
- fake `Source`;
- metadata informal.

---

### FR-EP-02 — Historical semantics

Episode MUST representar experiência histórica.

Um Episode pode continuar válido historicamente mesmo quando:

- uma decisão foi revertida;
- uma solução foi substituída;
- uma hipótese foi rejeitada;
- o estado atual mudou.

---

### FR-EP-03 — Explicit write

Na primeira versão, Episode MUST ser criado explicitamente por um caller autorizado pelo contrato existente da instância.

O Sofias Memory MUST NOT decidir autonomamente que uma SessionEntry deve virar Episode.

---

### FR-EP-04 — Scope

Todo Episode MUST pertencer explicitamente a um contexto lógico suportado pela capability.

No escopo inicial:

```text
project:<key>
```

---

### FR-EP-05 — Provenance

Todo Episode derivado de informação existente MUST possuir provenance/evidence suficiente para rastrear sua origem.

---

### FR-EP-06 — Human-readable context

Um Episode SHOULD possuir uma representação textual suficientemente clara para ser interpretada posteriormente por consumidores humanos ou de IA.

---

### FR-EP-07 — Historical queryability

Episodes MUST permanecer recuperáveis por consultas históricas e contextuais enquanto estiverem disponíveis segundo seu lifecycle.

---

### FR-EP-08 — No implicit current truth

O sistema MUST NOT promover automaticamente o conteúdo de um Episode para current semantic truth.

---

### FR-EP-09 — Relationship to Cognitive Memory

Quando um Episode resultar em uma verdade semântica durável, essa verdade MAY existir separadamente como `MemoryItem.semantic`.

Essa associação MUST NOT transformar Episode e MemoryItem no mesmo recurso.

---

### FR-EP-10 — Source independence

Um Episode MAY possuir múltiplas evidências independentes.

A perda de uma evidência MUST NOT necessariamente invalidar o Episode quando existirem outras evidências suficientes.

---

## 8. ContextCheckpoint Requirements

### FR-CP-01 — First-class resource

ContextCheckpoint MUST ser um recurso first-class.

Não deve depender exclusivamente de:

- Session metadata;
- arbitrary JSON conventions;
- `MemoryItem`;
- last SessionEntry.

---

### FR-CP-02 — Durable scope ownership

ContextCheckpoint MUST pertencer ao durable project scope.

Session MAY ser evidência.

Session MUST NOT ser o owner exclusivo do checkpoint.

---

### FR-CP-03 — Single current checkpoint

Para um mesmo contexto lógico, MUST existir no máximo:

```text
1 ACTIVE ContextCheckpoint
```

---

### FR-CP-04 — Explicit current state

O checkpoint current MUST ser determinado por lifecycle explícito.

O produto MUST NOT depender apenas de:

```text
MAX(created_at)
```

ou de política equivalente a:

```text
latest timestamp wins
```

---

### FR-CP-05 — Supersession

Uma nova revisão de continuidade MUST substituir explicitamente o checkpoint current anterior.

---

### FR-CP-06 — Historical checkpoints

Checkpoints superseded SHOULD permanecer disponíveis para inspeção histórica enquanto seu lifecycle permitir.

---

### FR-CP-07 — Continuation data

Um checkpoint MUST ser capaz de representar, quando aplicável:

- objective;
- current state;
- pending work;
- open items;
- blockers;
- next action.

Nem todos os campos precisam ser obrigatórios em todos os casos.

---

### FR-CP-08 — Evidence

Checkpoint derivado de informações existentes MUST manter evidências suficientes para explicar sua composição.

---

### FR-CP-09 — No task authority

Campos como:

```text
pending
next_action
blocker
```

representam contexto.

Eles MUST NOT modificar automaticamente sistemas externos de tasks ou workflows.

---

### FR-CP-10 — Restart persistence

O checkpoint current MUST permanecer recuperável após restart da aplicação e do Sofias Memory.

---

## 9. Checkpoint Concurrency Requirements

### FR-CONC-01

A substituição do checkpoint current MUST evitar dois estados ACTIVE simultâneos para o mesmo contexto lógico.

---

### FR-CONC-02

O produto MUST oferecer uma forma de detectar tentativa de atualização baseada em estado stale.

---

### FR-CONC-03

Quando um caller tentar substituir um checkpoint que não é mais current, a operação MUST falhar de forma detectável.

---

### FR-CONC-04

A falha de concorrência MUST NOT criar um segundo current checkpoint.

---

### FR-CONC-05

Retries seguros MUST NOT criar revisões duplicadas quando a mesma operação já tiver sido aplicada.

A estratégia exata de idempotência será definida em Contract/Spec.

---

## 10. Contextual Recall Requirements

### FR-CR-01 — Explicit contextual recall capability

O produto MUST oferecer uma capability dedicada à recuperação de Project Context.

---

### FR-CR-02 — Multiple memory planes

Contextual Recall MUST ser capaz de considerar mais de um plano de memória.

No escopo desta evolução:

- current Cognitive Memory;
- Episodes;
- current ContextCheckpoint;
- Session evidence;
- Knowledge Memory, quando relevante.

---

### FR-CR-03 — Selective retrieval

Contextual Recall MUST NOT obrigatoriamente recuperar todos os registros existentes no scope.

---

### FR-CR-04 — Current checkpoint preference

Quando a consulta envolver retomada de estado, o current ContextCheckpoint SHOULD ser considerado evidência prioritária.

---

### FR-CR-05 — Historical relevance

Quando a consulta envolver:

```text
why
when
what happened
what did we decide before
```

Episodes SHOULD poder participar da recuperação.

---

### FR-CR-06 — Current semantic truth

Consultas que dependam de estado semântico vigente SHOULD utilizar Cognitive Memory current truth quando disponível.

---

### FR-CR-07 — Provenance in result

Resultados contextuais MUST preservar informações suficientes para que o caller saiba de onde cada item foi recuperado.

---

### FR-CR-08 — No automatic prompt authority

Contextual Recall MUST retornar memória como contexto.

O resultado MUST NOT ser tratado pelo serviço como:

- system instruction;
- Policy;
- Permission;
- command.

---

### FR-CR-09 — Consumer-owned composition

O Sofias Memory MUST NOT assumir responsabilidade pela composição final da janela de contexto do LLM.

---

### FR-CR-10 — No mandatory LLM answer generation

Contextual Recall MUST poder fornecer contexto estruturado sem exigir geração de uma resposta final por LLM.

---

## 11. Session Integration Requirements

### FR-SES-01

Sessions existentes MUST permanecer compatíveis.

---

### FR-SES-02

SessionEntry MUST continuar append-only conforme seu contrato atual.

---

### FR-SES-03

SessionEntry MUST NOT ser automaticamente promovida para:

- MemoryItem;
- Episode;
- ContextCheckpoint.

---

### FR-SES-04

Episode MAY referenciar uma ou mais SessionEntries como Evidence.

---

### FR-SES-05

ContextCheckpoint MAY referenciar uma ou mais Sessions ou SessionEntries como Evidence.

---

### FR-SES-06

A ausência de Session MUST NOT impedir necessariamente a criação de Episode ou ContextCheckpoint quando a provenance exigida puder ser satisfeita de outra forma.

---

## 12. Cognitive Memory Integration Requirements

### FR-CM-01

O contrato atual de `MemoryItem` MUST continuar suportando somente os tipos vigentes do baseline:

```text
profile
semantic
```

---

### FR-CM-02

Este PRD MUST NOT adicionar:

```text
episodic
```

como `memory_type`.

---

### FR-CM-03

Episode e MemoryItem MAY coexistir sobre o mesmo tema.

---

### FR-CM-04

Quando uma decisão histórica deixar de representar current truth:

- o Episode histórico pode permanecer;
- a Cognitive Memory deverá seguir sua própria semântica de supersession.

---

### FR-CM-05

Contextual Recall SHOULD conseguir diferenciar explicitamente:

```text
historical episode
```

de:

```text
current semantic memory
```

---

## 13. Knowledge Memory Integration Requirements

### FR-KM-01

Knowledge Memory existente MUST continuar independente.

---

### FR-KM-02

Documents, Sources e Chunks MUST NOT ser substituídos por Episodes.

---

### FR-KM-03

Episode MAY referenciar Knowledge Memory como Evidence.

---

### FR-KM-04

Contextual Recall MAY utilizar Knowledge Memory quando a consulta exigir material source-backed.

---

### FR-KM-05

A nova capability MUST NOT mudar silenciosamente os modos atuais de Knowledge Recall.

---

## 14. Provenance Requirements

### FR-PROV-01

Episode derivado de outros recursos MUST registrar Evidence references.

---

### FR-PROV-02

ContextCheckpoint derivado de outros recursos MUST registrar Evidence references.

---

### FR-PROV-03

Evidence MUST permitir distinguir pelo menos:

```text
source resource
source identity
availability
```

quando aplicável.

---

### FR-PROV-04

Um caller MUST ser capaz de diferenciar conteúdo:

- explicitamente fornecido;
- derivado;
- inferido, caso inferência seja futuramente permitida pelo contrato.

---

### FR-PROV-05

A provenance MUST ser suficiente para suportar lifecycle relacionado a Forget.

---

### FR-PROV-06

A provenance SHOULD permitir explainability futura sem exigir armazenamento duplicado de toda a fonte original.

---

## 15. Forget Requirements

### FR-FGT-01 — No resurrection

Conteúdo corretamente esquecido MUST NOT reaparecer em contextual recall através de derivado que depende exclusivamente desse conteúdo.

---

### FR-FGT-02 — Evidence invalidation

Quando Evidence deixar de estar disponível devido a Forget, derivados relacionados MUST ser reavaliáveis.

---

### FR-FGT-03 — Single-source derived content

Se um Episode ou ContextCheckpoint depender exclusivamente de conteúdo esquecido, ele MUST deixar de disponibilizar a informação derivada correspondente.

---

### FR-FGT-04 — Independent support

Se um derivado possuir evidências independentes suficientes, Forget de uma evidência MAY permitir que o derivado permaneça disponível.

---

### FR-FGT-05 — No blind cascade

O produto MUST NOT exigir cascade deletion cego de todos os derivados relacionados.

---

### FR-FGT-06 — Provenance update

Quando um derivado permanecer após perda de uma Evidence, sua provenance MUST refletir corretamente a indisponibilidade ou remoção da evidência anterior.

---

### FR-FGT-07 — No hidden duplicate

A implementação MUST NOT preservar cópias recuperáveis de conteúdo esquecido em campos destinados apenas a facilitar derivação ou debugging.

---

## 16. Supervised-First Requirements

### FR-SUP-01

A criação inicial de Episode MUST ser uma operação explícita.

---

### FR-SUP-02

A criação inicial de ContextCheckpoint MUST ser uma operação explícita.

---

### FR-SUP-03

O serviço MUST NOT realizar automatic distillation no escopo inicial.

---

### FR-SUP-04

O serviço MUST NOT realizar automatic contradiction resolution no escopo inicial.

---

### FR-SUP-05

O serviço MUST NOT modificar current Cognitive Memory como efeito colateral da criação de Episode.

---

### FR-SUP-06

O serviço MUST NOT criar Episode como efeito colateral silencioso de Recall.

---

## 17. Lifecycle Requirements

### FR-LC-01

Episode MUST possuir lifecycle explícito.

Os estados exatos serão definidos em Spec.

---

### FR-LC-02

ContextCheckpoint MUST possuir lifecycle explícito.

No mínimo, a semântica deverá suportar:

```text
current
historical
```

sem depender apenas de timestamp.

---

### FR-LC-03

Lifecycle de Episode MUST NOT copiar automaticamente a semântica de `MemoryItem.supersede`.

---

### FR-LC-04

Um Episode histórico não deve se tornar inválido apenas porque existe Episode mais recente.

---

### FR-LC-05

Um ContextCheckpoint anterior MUST deixar de ser current quando uma nova revisão for aceita.

---

### FR-LC-06

A transição entre checkpoints MUST ser auditável.

---

## 18. Historical Semantics Requirements

### FR-HIST-01

O produto MUST preservar a distinção entre:

```text
what was decided
```

e:

```text
what is currently true
```

---

### FR-HIST-02

Uma mudança de decisão SHOULD poder resultar em:

```text
old Episode remains historical
old MemoryItem becomes superseded
new MemoryItem becomes current
new Episode records change
```

quando o caller decidir persistir todos esses elementos.

---

### FR-HIST-03

Consultas históricas MUST NOT ser respondidas exclusivamente com current checkpoint quando houver Episodes relevantes.

---

### FR-HIST-04

Consultas sobre current state MUST NOT tratar uma decisão histórica superseded como current truth.

---

## 19. Restart and Durability Requirements

### NFR-DUR-01

Episodes MUST sobreviver a restart do serviço.

---

### NFR-DUR-02

ContextCheckpoints MUST sobreviver a restart do serviço.

---

### NFR-DUR-03

Lifecycle e provenance MUST ser persistidos de forma durável.

---

### NFR-DUR-04

O estado current de checkpoint MUST ser reconstruível exclusivamente a partir do armazenamento autoritativo.

---

### NFR-DUR-05

Nenhuma informação necessária para determinar o checkpoint current pode existir apenas em cache de processo.

---

## 20. Consistency Requirements

### NFR-CON-01

PostgreSQL MUST permanecer autoridade de dados para os novos recursos persistentes.

---

### NFR-CON-02

A existência ou indisponibilidade de Neo4j MUST NOT impedir o funcionamento básico de Episodes e ContextCheckpoints no primeiro escopo.

---

### NFR-CON-03

A operação de substituição do checkpoint current MUST ser consistente mesmo sob concorrência.

---

### NFR-CON-04

O sistema MUST fail closed quando não puder determinar de forma segura qual checkpoint é current após conflito de escrita.

---

## 21. Security and Trust Requirements

### NFR-SEC-01

Project scope MUST NOT ser interpretado como autorização.

---

### NFR-SEC-02

A capability MUST continuar respeitando o modelo de autenticação vigente da instância enquanto esse baseline permanecer válido.

---

### NFR-SEC-03

Conteúdo recuperado MUST ser tratado como untrusted context.

---

### NFR-SEC-04

Memory content MUST NOT alterar:

- Policies;
- Permissions;
- system instructions;
- execution authority.

---

### NFR-SEC-05

Evidence de origem externa MUST NOT ganhar autoridade adicional apenas por ter sido persistida.

---

## 22. Compatibility Requirements

### NFR-COMP-01

A evolução MUST ser aditiva para consumidores existentes.

---

### NFR-COMP-02

Consumidores que conhecem somente:

- Knowledge Memory;
- Sessions;
- Cognitive Memory;

MUST continuar funcionando.

---

### NFR-COMP-03

O serviço SHOULD anunciar novas capabilities por mecanismo explícito de compatibility/capability negotiation, consistente com o padrão já usado pelo projeto.

---

### NFR-COMP-04

Consumidores MUST NOT precisar inferir suporte à nova capability apenas pela versão geral da aplicação.

---

## 23. Observability Requirements

### NFR-OBS-01

Criação e supersession de ContextCheckpoint SHOULD ser auditáveis.

---

### NFR-OBS-02

Criação e lifecycle de Episode SHOULD ser auditáveis.

---

### NFR-OBS-03

Contextual Recall SHOULD permitir identificar quais classes de memória contribuíram para o resultado.

---

### NFR-OBS-04

Falhas de lifecycle, concurrency e provenance SHOULD ser distinguíveis operacionalmente.

---

## 24. Contextual Retrieval Quality Requirements

### NFR-RET-01

O produto MUST permitir recuperar Project Context sem carregar toda a memória do projeto.

---

### NFR-RET-02

Contextual Recall SHOULD minimizar recuperação de informação irrelevante.

---

### NFR-RET-03

Consultas de retomada SHOULD favorecer current checkpoint.

---

### NFR-RET-04

Consultas causais SHOULD conseguir recuperar Episodes relevantes.

---

### NFR-RET-05

Consultas de verdade vigente SHOULD favorecer current Cognitive Memory sobre material puramente histórico.

---

### NFR-RET-06

O ranking final não será definido neste PRD.

---

## 25. Primary User Journeys

---

### Journey 1 — Retomar trabalho

#### Given

Um projeto possui:

- Sessions anteriores;
- MemoryItems;
- Episodes;
- um current ContextCheckpoint.

#### When

Uma nova Session pergunta:

```text
Onde paramos?
```

#### Then

O sistema deve fornecer contexto suficiente para o caller identificar:

- objective;
- current state;
- pending items;
- next action.

Não deve exigir replay integral do histórico.

---

### Journey 2 — Explicar uma decisão

#### Given

Existe current semantic memory e Episode relacionado à decisão.

#### When

O caller pergunta:

```text
Por que essa decisão foi tomada?
```

#### Then

Contextual Recall deve poder retornar:

- current semantic state;
- Episode relevante;
- Evidence associada.

---

### Journey 3 — Recuperar decisão anterior

#### Given

Uma decisão foi substituída posteriormente.

#### When

O caller pergunta:

```text
O que havíamos decidido antes?
```

#### Then

Episodes históricos devem permanecer recuperáveis sem serem confundidos com current truth.

---

### Journey 4 — Atualizar continuidade

#### Given

Existe um checkpoint ACTIVE.

#### When

O caller persiste nova revisão com base no checkpoint current.

#### Then

O checkpoint anterior deixa de ser current e o novo se torna current atomicamente.

---

### Journey 5 — Conflito de checkpoint

#### Given

Dois callers leram o mesmo checkpoint current.

#### When

O primeiro cria nova revisão e o segundo tenta atualizar com base no estado antigo.

#### Then

A segunda operação deve ser rejeitada de forma detectável.

---

### Journey 6 — Forget de evidência única

#### Given

Um Episode depende exclusivamente de uma Evidence.

#### When

Essa Evidence é esquecida.

#### Then

O conteúdo derivado não pode continuar recuperável.

---

### Journey 7 — Forget com evidência independente

#### Given

Um Episode possui três evidências independentes.

#### When

Uma delas é esquecida.

#### Then

O Episode pode continuar disponível se as evidências restantes forem suficientes, com provenance atualizada.

---

## 26. MVP Scope

O MVP funcional desta evolução deverá entregar:

1. `Episode` first-class;
2. `ContextCheckpoint` first-class;
3. uso de `project:<key>` como contexto lógico inicial;
4. Evidence/provenance para ambos;
5. lifecycle explícito;
6. single current checkpoint;
7. checkpoint supersession;
8. concorrência segura na atualização;
9. Contextual Recall;
10. integração com current Cognitive Memory;
11. integração com Episodes;
12. integração com current ContextCheckpoint;
13. uso opcional de Session evidence;
14. uso opcional de Knowledge Memory;
15. provenance-aware Forget;
16. compatibility negotiation;
17. persistência durável;
18. restart recovery;
19. supervised-only writes.

---

## 27. Explicitly Deferred from MVP

Não pertencem ao MVP:

- automatic Episode extraction;
- automatic checkpoint generation;
- background session distillation;
- autonomous consolidation;
- automatic contradiction resolution;
- automatic project summarization;
- importance score;
- memory decay;
- MemorySpace;
- graph projection de Episodes;
- Episode-to-Episode graph traversal;
- multi-project inference;
- cross-project automatic recall;
- per-project authorization;
- multi-user model;
- Agent runtime integration específica.

---

## 28. Product Acceptance Criteria

A capability será considerada funcional quando todos os critérios abaixo forem demonstrados.

### AC-01

É possível persistir um Episode em `project:test`.

### AC-02

É possível recuperar esse Episode após restart.

### AC-03

É possível persistir um ContextCheckpoint em `project:test`.

### AC-04

É possível recuperar o current ContextCheckpoint após restart.

### AC-05

Criar nova revisão substitui corretamente o checkpoint anterior.

### AC-06

Nunca existem dois current checkpoints para o mesmo contexto lógico após operações concluídas com sucesso.

### AC-07

Concorrência stale é detectada.

### AC-08

Uma nova Session consegue recuperar continuidade de uma Session anterior sem replay integral.

### AC-09

Uma consulta contextual consegue distinguir current semantic memory de Episode histórico.

### AC-10

Uma consulta causal consegue recuperar Episode relevante quando ele existe.

### AC-11

Uma consulta restrita a `project:a` não retorna automaticamente memória de `project:b`.

### AC-12

SessionEntry não vira Episode automaticamente.

### AC-13

Episode não vira MemoryItem automaticamente.

### AC-14

Checkpoint não modifica task externa.

### AC-15

Contextual Recall não altera Policy ou Permission.

### AC-16

Forget de Evidence única impede recuperação do conteúdo derivado correspondente.

### AC-17

Forget de uma Evidence entre várias permite preservar derivado somente quando houver suporte independente suficiente.

### AC-18

Consumidores da API v0.7.0 continuam funcionando sem conhecer a nova capability.

---

## 29. Minimum Validation Scenario

### Phase A

Criar:

```text
scope = project:test
```

Persistir:

- Session A;
- SessionEntries;
- MemoryItem semantic;
- Episode;
- ContextCheckpoint.

Encerrar o processo.

---

### Phase B

Reiniciar o serviço e o consumidor.

Criar Session B.

Executar Contextual Recall.

O resultado deverá permitir reconstruir:

```text
objective
current decision
reason for decision
pending work
next action
```

sem replay integral da Session A.

---

### Phase C

Criar nova revisão do ContextCheckpoint.

Validar:

```text
old checkpoint = historical
new checkpoint = current
```

---

### Phase D

Executar tentativa concorrente baseada no checkpoint antigo.

Validar rejeição.

---

### Phase E

Forget de Evidence utilizada pelo Episode.

Validar comportamento de provenance-aware invalidation.

---

## 30. Risks and Constraints

### R-01 — Excessive duplication

Episode e Checkpoint podem se tornar cópias de Sessions.

**Requirement:** preservar síntese/contexto e Evidence, não transcript integral por padrão.

---

### R-02 — Current truth confusion

Historical Episode pode ser tratado como current semantic truth.

**Requirement:** manter tipos e respostas distinguíveis.

---

### R-03 — Stale checkpoint

Estado antigo pode sobrescrever atualização mais recente.

**Requirement:** concorrência explícita e detectável.

---

### R-04 — Forget leakage

Informação esquecida pode sobreviver em derivado.

**Requirement:** provenance-aware invalidation.

---

### R-05 — Scope leakage

Contextual Recall pode misturar projetos.

**Requirement:** scope matching explícito.

---

### R-06 — Retrieval noise

Recuperação composta pode retornar contexto demais.

**Requirement:** retrieval seletivo; algoritmo será definido posteriormente.

---

### R-07 — Product drift

A capability pode crescer em direção a Project Manager ou Agent Runtime.

**Requirement:** preservar Product Boundaries aprovados.

---

## 31. Product Boundaries

| Responsibility | Owner |
|---|---|
| Knowledge persistence | Sofias Memory |
| Cognitive Memory persistence | Sofias Memory |
| Episode persistence | Sofias Memory |
| ContextCheckpoint persistence | Sofias Memory |
| Provenance | Sofias Memory |
| Lifecycle | Sofias Memory |
| Contextual Recall | Sofias Memory |
| Candidate extraction | Consumer |
| Decision to persist Episode | Consumer |
| Decision to persist Checkpoint | Consumer |
| Conversation UX | Consumer |
| Prompt composition | Consumer |
| Agent planning | Consumer/runtime |
| Tool execution | Consumer/runtime |
| Task authority | External system |
| Permissions | Consumer/runtime |
| Workflow execution | External runtime |

---

## 32. Approved Product Rules

### PRD-RULE-01

`MemoryItem` continua `profile|semantic`.

### PRD-RULE-02

`Episode` é first-class e separado.

### PRD-RULE-03

Episode representa histórico.

### PRD-RULE-04

ContextCheckpoint representa continuidade.

### PRD-RULE-05

ContextCheckpoint pertence ao durable scope.

### PRD-RULE-06

Existe no máximo um current checkpoint por contexto lógico.

### PRD-RULE-07

Current checkpoint muda por supersession explícita.

### PRD-RULE-08

SessionEntry permanece contexto temporal.

### PRD-RULE-09

Nenhuma promoção automática ocorre no MVP.

### PRD-RULE-10

Contextual Recall é composto, não uma fusão dos domínios existentes.

### PRD-RULE-11

Memory permanece contexto não confiável para fins de autoridade.

### PRD-RULE-12

Forget deverá respeitar provenance de derivados.

### PRD-RULE-13

`project:<key>` é contexto, não autorização.

### PRD-RULE-14

PostgreSQL permanece autoridade.

### PRD-RULE-15

Neo4j não é requisito funcional para o MVP contextual.

---

## 33. Open Questions

Nenhuma questão de produto crítica permanece aberta neste PRD.

As seguintes decisões são intencionalmente deixadas para documentos posteriores:

- schema exato de `Episode`;
- schema exato de `ContextCheckpoint`;
- lifecycle enum final;
- endpoint structure;
- payloads;
- error codes;
- idempotency contract;
- evidence model;
- invalidation algorithm;
- contextual ranking;
- lexical/vector/hybrid combination;
- token budgeting;
- query planning;
- indexes;
- migrations;
- Neo4j future projection;
- version do feature contract.

Esses itens deverão ser resolvidos em Specs, Contracts, Architecture e ADRs, sem alterar silenciosamente as regras deste PRD.
