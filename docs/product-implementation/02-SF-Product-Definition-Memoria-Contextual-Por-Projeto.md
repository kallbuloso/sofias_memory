# Sofias Memory — Product Definition: Memória Contextual Persistente por Projeto

**Document:** `02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md`\
**Status:** APPROVED\
**Date:** 2026-11-17\
**Project:** Sofias Memory\
**Current phase:** Product Definition\
**Current role:** Product Engineer\
**Baseline:** `01-SF-Discovery-Memoria-Contextual-Por-Projeto.md` — APPROVED

---

## 1. Product Vision

O Sofias Memory deverá evoluir de uma infraestrutura capaz de armazenar conhecimento, fatos, preferências e histórico temporal para uma infraestrutura capaz de também preservar **continuidade contextual duradoura**.

A evolução permitirá que aplicações e assistentes mantenham memória persistente sobre um projeto ao longo de múltiplas sessões, conversas, reinicializações e períodos de tempo.

A proposta não transforma o Sofias Memory em um assistente, planner, runtime de agentes ou sistema de gerenciamento de projetos.

O produto continuará sendo uma infraestrutura de memória.

A capability proposta adicionará a possibilidade de responder, de forma recuperável e auditável, perguntas como:

```text id="4bw8yt"
O que sabemos sobre este projeto?

O que aconteceu anteriormente?

Por que determinada decisão foi tomada?

O que mudou desde então?

Qual decisão continua vigente?

Onde o trabalho foi interrompido?

Qual é o próximo ponto de continuidade?
```

---

## 2. Product Definition

> **Sofias Memory Contextual Project Memory** permite que aplicações, assistentes e agentes preservem experiências relevantes, decisões históricas e estado de continuidade associados a um projeto, para que o contexto necessário possa ser recuperado posteriormente sem depender do replay integral de conversas anteriores.

A capability deverá complementar, e não substituir:

- Knowledge Memory;
- Native Cognitive Memory;
- Sessions;
- SessionEntries;
- Skills;
- Agent Profiles.

---

## 3. Problem

A versão atual do Sofias Memory consegue preservar:

- documentos e conhecimento;
- fatos duráveis;
- preferências;
- decisões semanticamente persistentes;
- verdade temporal;
- histórico temporal de sessões.

Entretanto, esses elementos não formam, por si só, uma representação suficiente da **experiência acumulada de um projeto**.

O produto ainda não representa first-class:

- acontecimentos relevantes;
- histórico de mudança de decisões;
- justificativas;
- resultados de investigações;
- continuidade entre sessões;
- estado de retomada.

Isso faz com que aplicações consumidoras precisem reconstruir contexto a partir de:

- mensagens antigas;
- SessionEntries;
- memórias semânticas isoladas;
- metadata;
- lógica própria.

Essa reconstrução é repetitiva, inconsistente e tende a se tornar mais difícil conforme o projeto cresce.

---

## 4. Target Users / Consumers

O Sofias Memory permanece uma infraestrutura destinada a consumidores técnicos.

### 4.1 Assistentes conversacionais

Assistentes de longo prazo que precisam manter continuidade entre conversas.

Exemplos:

- assistentes pessoais;
- assistentes de desenvolvimento;
- copilotos de projeto;
- assistentes internos.

Necessidade principal:

> iniciar uma nova conversa sem perder o estado contextual das anteriores.

---

### 4.2 Agentes

Agentes podem preservar experiências relevantes de execuções anteriores.

Exemplos:

- investigação de bugs;
- pesquisas;
- análise de dados;
- workflows técnicos;
- resolução de incidentes.

Necessidade principal:

> recuperar aprendizados e resultados anteriores sem repetir trabalho desnecessariamente.

O Sofias Memory não executará esses agentes.

---

### 4.3 Aplicações

Aplicações podem utilizar a capability para preservar contexto de longo prazo relacionado a:

- projetos;
- casos;
- investigações;
- processos;
- atividades recorrentes;
- atendimentos de longa duração.

---

### 4.4 Automations

Automations independentes podem utilizar o Sofias Memory como estado contextual compartilhado entre execuções.

Exemplo:

```text id="x3mjdv"
Automation A
    ↓
Sofias Memory
    ↓
Automation B
```

desde que ambas utilizem explicitamente o mesmo contexto lógico.

---

## 5. Value Proposition

A principal proposta de valor é:

> permitir que uma aplicação recupere o contexto necessário para continuar um trabalho anterior sem carregar todo o histórico bruto e sem depender da memória do usuário.

O benefício esperado não é simplesmente aumentar a quantidade de dados armazenados.

É aumentar a **qualidade da continuidade**.

### 5.1 Para o usuário final do assistente

Reduzir:

- necessidade de recapitulacao manual;
- repetição de decisões;
- perda de contexto;
- investigações duplicadas;
- inconsistência entre sessões.

---

### 5.2 Para a aplicação consumidora

Reduzir a necessidade de implementar internamente:

- project memory ad-hoc;
- resumos manuais;
- convenções de metadata;
- mecanismos próprios de continuidade;
- reconstrução de contexto a partir de históricos longos.

---

### 5.3 Para o Sofias Memory

Evoluir de:

```text id="u2n6ww"
storage + retrieval
```

para:

```text id="t0etcn"
durable contextual memory infrastructure
```

sem assumir responsabilidades de agent runtime.

---

## 6. Core Concepts

A Product Definition adota cinco conceitos centrais.

---

## 6.1 Project Memory

**Project Memory** representa o conjunto persistente de informações relacionadas a um contexto duradouro de projeto.

Pode incluir:

- Knowledge Memory;
- Cognitive Memory;
- Sessions;
- Episodes;
- Context Checkpoints.

Project Memory é um conceito lógico.

Nesta fase, não implica obrigatoriamente uma entidade física chamada `Project` ou `MemorySpace`.

---

## 6.2 Project Context

**Project Context** é a seleção contextual necessária para compreender ou continuar uma atividade naquele momento.

Diferença:

```text id="jggc23"
Project Memory
    = tudo que foi preservado

Project Context
    = o que é relevante agora
```

O Project Context poderá ser reconstruído a partir da Project Memory.

---

## 6.3 Episode

`Episode` representa uma experiência histórica relevante.

Exemplos:

- decisão;
- investigação;
- experimento;
- incidente;
- mudança de direção;
- descoberta;
- resultado;
- aprendizado;
- marco relevante.

### Regra fundamental

> Episode registra que algo aconteceu, não que aquilo continua sendo verdade vigente.

Exemplo:

```text id="zhxdq8"
Episode:
"Foi decidido utilizar arquitetura A."
```

continua historicamente correto mesmo depois de:

```text id="9uv9em"
MemoryItem.semantic:
"A arquitetura vigente é B."
```

---

## 6.4 ContextCheckpoint

`ContextCheckpoint` representa o estado de continuidade de um contexto duradouro.

Ele responde principalmente:

```text id="9rti4w"
Onde estamos?
```

Pode conter conceitualmente:

- objetivo atual;
- estado conhecido;
- decisões relevantes;
- pendências;
- blockers;
- próxima ação;
- referências para evidências.

### Regra fundamental

> ContextCheckpoint representa memória contextual, não autoridade operacional.

Se um checkpoint indicar:

```text id="cl0s8u"
next_action = "Implementar feature X"
```

isso não significa que o Sofias Memory controla a task correspondente.

---

## 6.5 Contextual Recall

Contextual Recall representa a capability de recuperar evidências provenientes de diferentes planos de memória para reconstruir contexto.

Pode envolver:

```text id="s030vu"
Cognitive Memory
+
Episodes
+
ContextCheckpoint
+
Session evidence
+
Knowledge Memory
```

O Sofias Memory deverá retornar contexto e evidências.

A composição final do prompt continuará pertencendo ao consumidor.

---

## 7. Core Workflows

---

## 7.1 Registrar contexto temporal

Uma aplicação cria ou utiliza uma Session.

Durante a interação:

```text id="a7kvrn"
Session
    ↓
SessionEntry
    ↓
SessionEntry
    ↓
SessionEntry
```

Esse fluxo permanece como já existe.

Nenhuma SessionEntry se transforma automaticamente em memória permanente.

---

## 7.2 Registrar memória cognitiva

Quando uma informação deve permanecer como fato, preferência ou conhecimento semântico:

```text id="1g81jy"
approved candidate
      ↓
MemoryItem
```

Esse fluxo continua sendo responsabilidade da Native Cognitive Memory.

---

## 7.3 Registrar uma experiência

Quando um acontecimento tiver valor contextual duradouro, o caller poderá persistir um:

```text id="1ohdzt"
Episode
```

Exemplos:

```text id="jpcpjj"
investigação concluída
decisão relevante
mudança arquitetural
experimento
incidente
descoberta
```

O Episode deverá preservar referências para as evidências que o sustentam.

---

## 7.4 Registrar continuidade

Quando houver um estado significativo de continuidade:

```text id="ca49dp"
ContextCheckpoint
```

poderá ser persistido.

Exemplo:

```text id="fkbv05"
Objective:
Definir recuperação contextual.

Current state:
Modelo conceitual aprovado.

Open:
Contrato de recuperação.

Next:
Criar PRD.
```

---

## 7.5 Atualizar continuidade

Quando o estado do trabalho mudar:

```text id="l5cpqs"
Checkpoint A ACTIVE
        ↓
supersede
        ↓
Checkpoint B ACTIVE
```

Apenas um checkpoint pode representar o estado atual de um mesmo contexto lógico.

---

## 7.6 Retomar projeto

Uma aplicação inicia nova Session.

Em vez de reprocessar todo o histórico:

```text id="0ny3at"
new Session
     ↓
contextual recall
     ↓
current checkpoint
relevant episodes
current semantic memory
relevant knowledge
     ↓
Project Context
```

O caller utiliza o contexto resultante conforme sua própria política.

---

## 7.7 Recuperar justificativa histórica

Consulta:

```text id="mgoqj9"
Por que escolhemos a estratégia atual?
```

A recuperação poderá encontrar:

```text id="u4c5rf"
current semantic decision
+
Episode da decisão
+
evidências originais
```

Isso permite distinguir:

```text id="6q5ghc"
what is true
```

de:

```text id="fcyjfn"
why it became true
```

---

## 7.8 Consultar estado histórico

Consulta:

```text id="7v70c0"
O que havíamos decidido antes dessa mudança?
```

Episodes poderão fornecer o histórico sem substituir a verdade vigente.

---

## 7.9 Forget com conteúdo derivado

Quando uma evidência for esquecida:

```text id="xfl16h"
Forget evidence
      ↓
derived records evaluated
```

Registros derivados sem evidência independente suficiente não poderão continuar expondo uma paráfrase recuperável do conteúdo esquecido.

A semântica detalhada será definida posteriormente em Spec e Contract.

---

## 8. Initial Product Scope

A primeira versão da capability deverá incluir:

### IN SCOPE

- Project Memory como modelo lógico;
- uso de `project:<key>` como contexto lógico inicial;
- `Episode` first-class;
- `ContextCheckpoint` first-class;
- linear checkpoint supersession;
- no máximo um checkpoint ACTIVE por contexto lógico;
- proveniência de Episodes;
- proveniência de Checkpoints;
- associação de evidências;
- recuperação de checkpoint atual;
- recuperação contextual composta;
- separação clara entre current semantic truth e historical experience;
- integração com Cognitive Memory existente;
- integração com Sessions existentes;
- integração com Knowledge Memory existente quando relevante;
- supervised-first write;
- provenance-aware invalidation após Forget;
- compatibilidade aditiva com a API atual.

---

## 9. Out of Scope

Não pertence ao escopo inicial:

- Agent Runtime;
- planejamento;
- execução de tools;
- scheduler;
- task management;
- workflow engine genérico;
- prompt orchestration;
- provider routing;
- autonomous memory creation;
- autonomous supersession;
- autonomous contradiction resolution;
- automatic session distillation;
- automatic consolidation;
- multi-agent orchestration;
- tenancy;
- generic RBAC;
- ACL por project scope;
- per-project API keys;
- transformação de scope em security boundary;
- substituição da Native Cognitive Memory;
- substituição de Sessions;
- substituição de Knowledge Memory.

---

## 10. Future / Deferred

Podem ser avaliados após validação do núcleo.

### 10.1 Automatic Distillation

Analisar Session history e propor Episodes ou MemoryItems.

---

### 10.2 MemorySpace first-class

Uma entidade formal para organizar contextos duradouros.

Possíveis tipos futuros:

```text id="othit0"
project
case
research
topic
workflow
```

Não é necessária no MVP da capability.

---

### 10.3 Episode Graph Projection

Representação de relações entre:

- Episodes;
- decisões;
- entidades;
- projetos;
- causas;
- consequências.

Só deverá ser adicionada mediante benefício demonstrado.

---

### 10.4 Contradiction Detection

Detecção de possíveis conflitos entre memórias.

---

### 10.5 Reconciliation

Workflow supervisionado para resolver:

- duplicação;
- conflito;
- informação obsoleta;
- divergência temporal.

---

### 10.6 Context Importance

Ranking baseado em relevância, recência, feedback e importância.

Não deverá existir apenas como campo sem semântica definida.

---

### 10.7 Learning from Agent Outcomes

Utilização de resultados de execuções externas como evidência para futuras recuperações.

---

## 11. Product Rules

---

### PR-01 — Episode é histórico

Episode MUST representar experiência ou acontecimento.

Episode MUST NOT representar implicitamente current truth.

---

### PR-02 — MemoryItem permanece factual/semântico

A Native Cognitive Memory permanece:

```text id="qzlzd1"
profile
semantic
```

Esta evolução não adiciona `episodic` ao contrato atual.

---

### PR-03 — Session permanece temporal

Session e SessionEntry continuam representando contexto temporal.

SessionEntry MUST NOT ser automaticamente promovida para Episode ou MemoryItem.

---

### PR-04 — Checkpoint possui estado current explícito

Para um mesmo contexto lógico:

```text id="0u3h4x"
max ACTIVE checkpoints = 1
```

---

### PR-05 — Checkpoint utiliza supersession

Nova revisão relevante substitui explicitamente o checkpoint atual.

Não deverá existir apenas uma política implícita de:

```text id="19b9yk"
latest timestamp wins
```

---

### PR-06 — Scope organiza contexto

`project:<key>` representa contexto lógico.

Não representa:

- tenant;
- owner;
- ACL;
- autorização.

---

### PR-07 — Provenance é obrigatória

Episodes e Checkpoints derivados de evidências existentes devem preservar referências suficientes para rastrear sua origem.

---

### PR-08 — Forget não pode ser contornado

Conteúdo esquecido não pode permanecer disponível simplesmente porque foi resumido ou parafraseado em Episode ou Checkpoint.

---

### PR-09 — Evidência independente pode preservar derivado

Quando um derivado possuir múltiplas evidências independentes, Forget de uma delas não exige automaticamente destruição do derivado inteiro.

---

### PR-10 — Context não cria autoridade

Nenhum conteúdo recuperado poderá criar:

- Policy;
- Permission;
- system instruction;
- execução privilegiada.

---

### PR-11 — Caller controla persistência inicial

No primeiro escopo:

```text id="f3yob4"
explicit write
```

é obrigatório.

Sofias Memory não decide autonomamente quais acontecimentos merecem virar Episodes.

---

### PR-12 — Caller controla ContextBuilder

Sofias Memory recupera contexto.

O caller decide:

- o que entra no prompt;
- quanto contexto usar;
- como apresentar;
- como combinar com instruções e estado operacional.

---

## 12. Constraints

---

## 12.1 PostgreSQL authoritative

Toda nova memória durável deverá respeitar o modelo existente:

```text id="88ap1f"
PostgreSQL = source of truth
```

---

## 12.2 Neo4j não é requisito inicial

Episodes e Checkpoints não exigirão Neo4j para serem funcionais no primeiro incremento.

---

## 12.3 Single-user baseline

A capability permanece compatível com o modelo atual single-user da instância.

---

## 12.4 Backward compatibility

A evolução deve ser aditiva.

Endpoints existentes não deverão mudar de semântica por consequência da nova capability.

---

## 12.5 Existing cognitive contracts remain valid

O contrato v0.7.0 deve continuar funcionando sem que seus consumidores conheçam Episodes ou Checkpoints.

---

## 12.6 No hidden automation

Não deverá haver promoção automática de SessionEntry para:

```text id="f96hj3"
MemoryItem
Episode
ContextCheckpoint
```

sem contrato explícito futuro.

---

## 13. Product Success Criteria

A primeira versão deverá demonstrar valor através de continuidade real.

### SC-01 — Cross-session continuity

Uma Session B consegue recuperar o estado relevante produzido durante Session A.

---

### SC-02 — No full-history dependency

A retomada não depende de reenviar toda a Session anterior ao LLM.

---

### SC-03 — Current vs historical distinction

O sistema consegue diferenciar:

```text id="pe3d5f"
decisão atual
```

de:

```text id="3z1c07"
decisão histórica
```

---

### SC-04 — Causal retrieval

Quando a evidência existir, deve ser possível recuperar contexto suficiente para responder:

```text id="tp3qg6"
Por que essa decisão foi tomada?
```

---

### SC-05 — Continuation state

Deve ser possível recuperar:

```text id="o53k7h"
objective
current state
pending work
next action
```

a partir do checkpoint vigente.

---

### SC-06 — Restart persistence

A continuidade deve sobreviver a restart da aplicação e do consumidor.

---

### SC-07 — Scope isolation

Uma consulta restrita a:

```text id="60rpwa"
project:a
```

não deve misturar automaticamente memória contextual de:

```text id="yzsetn"
project:b
```

---

### SC-08 — Forget integrity

Conteúdo explicitamente esquecido não deve reaparecer através de um derivado sem suporte independente.

---

## 14. Minimum Product Experiment

O primeiro experimento deverá usar duas Sessions diferentes.

### Session A

Contexto:

```text id="806pxp"
scope = project:test
```

Fluxo:

1. criar Session;
2. registrar SessionEntries;
3. criar uma memória semântica relevante;
4. registrar um Episode;
5. registrar um ContextCheckpoint;
6. encerrar o processo.

---

### Session B

Executada posteriormente e em novo processo.

O consumer deverá conseguir recuperar, sem replay integral da Session A:

```text id="h1c0zt"
objective
decision
reason
pending item
next action
```

### Expected outcome

O caller deve ser capaz de reconstruir corretamente o Project Context.

---

## 15. Example Product Scenario

Projeto:

```text id="4bn42f"
project:sofias-memory
```

### Session 1

Discussão:

```text id="tsf6bw"
Precisamos decidir como representar memória episódica.
```

Alternativas analisadas:

```text id="ekurjl"
A. memory_type=episodic
B. Episode first-class
```

Resultado:

```text id="1sl1t8"
Episode first-class foi escolhido.
```

MemoryItem atual:

```text id="wov2m1"
A arquitetura de memória contextual utiliza Episode como agregado próprio.
```

Episode:

```text id="682mz6"
Durante o Discovery foram comparadas duas estratégias.
Episode first-class foi selecionado para preservar a distinção
entre experiência histórica e current semantic truth.
```

Checkpoint:

```text id="ehv98z"
objective:
  definir memória contextual

current_state:
  Discovery aprovado

pending:
  Product Definition

next_action:
  revisar Product Definition
```

---

### Session 2

Pergunta:

```text id="a5nu3o"
Onde paramos?
```

Contextual Recall poderá recuperar:

```text id="351huu"
ContextCheckpoint ACTIVE
```

Resposta possível pelo caller:

```text id="65nb5y"
O Discovery da memória contextual foi aprovado.
O próximo passo é revisar a Product Definition.
```

---

Pergunta:

```text id="67de4x"
Por que Episode não virou memory_type?
```

Contextual Recall poderá recuperar:

```text id="hd9g8x"
Episode da decisão
+
MemoryItem atual
+
evidence references
```

O caller poderá então explicar a decisão com contexto.

---

## 16. Product Boundaries

O produto deverá manter a seguinte separação:

| Capability | Owner |
|---|---|
| Knowledge persistence | Sofias Memory |
| Cognitive persistence | Sofias Memory |
| Episode persistence | Sofias Memory |
| ContextCheckpoint persistence | Sofias Memory |
| Provenance | Sofias Memory |
| Temporal lifecycle | Sofias Memory |
| Contextual retrieval | Sofias Memory |
| Conversation UX | Consumer |
| Candidate extraction | Consumer |
| Agent planning | Consumer |
| Tool execution | Consumer/runtime |
| Permission decisions | Consumer/runtime |
| Prompt composition | Consumer/ContextBuilder |
| Operational task state | External task system |

---

## 17. Approved Decisions

Inherited from Discovery:

### PD-01

Project Memory será um modelo lógico duradouro.

### PD-02

Project Memory e Project Context são conceitos distintos.

### PD-03

`Episode` será first-class e separado de `MemoryItem`.

### PD-04

Episode representa experiência histórica, não current truth.

### PD-05

`ContextCheckpoint` será first-class.

### PD-06

Checkpoint pertence ao durable scope/context, não a uma Session individual.

### PD-07

Por contexto lógico haverá no máximo um checkpoint ACTIVE.

### PD-08

Atualização de checkpoint utilizará supersession explícita.

### PD-09

A evolução terá contextual recall composto.

### PD-10

A composição final do prompt permanece externa.

### PD-11

Primeira implementação será supervised-first.

### PD-12

Forget de evidência deve afetar derivados de maneira provenance-aware.

### PD-13

Checkpoint não representa autoridade sobre Tasks ou execução.

### PD-14

`project:<key>` permanece namespace contextual, não authorization boundary.

---

## 18. Open Questions

Nenhuma questão funcional crítica do Product Definition permanece aberta.

Detalhes ainda não definidos pertencem às próximas fases.

---

## 19. Deferred Decisions

Os seguintes itens não bloqueiam Product Definition:

- estrutura física de `Episode`;
- estrutura física de `ContextCheckpoint`;
- endpoints;
- schemas;
- error codes;
- ranking;
- indexes;
- token budgeting;
- Neo4j projection;
- distillation;
- automatic consolidation;
- MemorySpace;
- contradiction resolution;
- importance scoring.

Eles deverão ser tratados, quando necessário, em:

```text id="ez1vt3"
PRD
Specs
Contracts
Architecture
ADRs
```

---

## 20. Product Risks

### PRISK-01 — Contextual memory virar duplicação de chat history

Mitigação conceitual:

> Episodes e Checkpoints devem preservar contexto útil, não replicar transcripts.

---

### PRISK-02 — Contextual memory virar task management

Mitigação:

> Checkpoint registra estado contextual, não estado operacional autoritativo.

---

### PRISK-03 — Episode virar outro MemoryItem genérico

Mitigação:

> manter separação explícita entre historical experience e semantic truth.

---

### PRISK-04 — Contextual Recall virar um endpoint que busca tudo

Mitigação:

> retrieval deverá permanecer seletivo e contextual.

---

### PRISK-05 — Automação precoce contaminar memória

Mitigação:

> supervised-first.

---

### PRISK-06 — Forget inconsistente entre planos

Mitigação:

> proveniência e regras de invalidation fazem parte do produto, não são detalhe de implementação.

---

## 21. What This Product Is

Sofias Memory Contextual Project Memory é:

> infraestrutura persistente para preservar e recuperar continuidade contextual de longo prazo.

É capaz de representar conceitualmente:

```text id="7b60ej"
what is known
what happened
where we are
why we got here
```

---

## 22. What This Product Is Not

Não é:

```text id="fd626i"
Project Manager
Task Manager
Agent Runtime
Workflow Engine
Prompt Framework
LLM Gateway
Autonomous Planner
Authorization Engine
Conversation UI
```

---

## 23. Product Definition Statement

A definição consolidada do produto é:

> **Sofias Memory deverá permitir que aplicações preservem e recuperem memória contextual de longo prazo associada a projetos, distinguindo conhecimento vigente, experiências históricas, contexto temporal e estado de continuidade.**
>
> Essa capability deverá permitir que uma nova sessão compreenda onde um trabalho foi interrompido, quais decisões foram tomadas e por que, sem reprocessar todo o histórico anterior e sem transformar o Sofias Memory em runtime de agentes ou autoridade operacional.

---

## 24. Criteria to Advance to PRD

A Product Definition estará pronta para avançar quando houver aprovação explícita sobre:

1. visão do produto;
2. problema central;
3. target consumers;
4. proposta de valor;
5. `Project Memory`;
6. `Project Context`;
7. `Episode`;
8. `ContextCheckpoint`;
9. Contextual Recall;
10. initial scope;
11. out of scope;
12. product rules;
13. product boundaries;
14. success criteria;
15. minimum product experiment.

Não é necessário definir nesta fase:

- banco;
- migration;
- endpoints;
- schemas;
- ranking;
- algoritmo de retrieval;
- error model;
- modelo de embeddings;
- índices;
- Neo4j projection.

---

## 25. Project State

**Project:** Sofias Memory — Contextual Project Memory\
**Current phase:** Product Definition\
**Current role:** Product Engineer\
**Current artifact:** `02-SF-Product-Definition-Memoria-Contextual-Por-Projeto.md`\
**Artifact status:** APPROVED

### Approved baseline

- Discovery concluído e aprovado.
- PostgreSQL authoritative.
- Neo4j rebuildable projection.
- Sessions permanecem contexto temporal.
- `MemoryItem = profile|semantic`.
- Episode separado de MemoryItem.
- ContextCheckpoint first-class.
- checkpoint pertence ao durable scope.
- um ACTIVE checkpoint por contexto.
- checkpoint supersession explícita.
- Contextual Recall composto.
- supervised-first.
- provenance-aware Forget.
- Memory não representa operational authority.

### Open

Nenhuma decisão de produto crítica identificada neste documento.

### Deferred

- MemorySpace;
- graph projection;
- distillation;
- automated consolidation;
- contradiction resolution;
- importance scoring;
- tenancy/RBAC.

### Subsequent approved artifacts

PRD, Spec, Contract, Architecture, ADRs e Technical Backlog estão em `03–08`.
A autorização de implementação depende do Slice CURRENT em `AGENTS.md`.
