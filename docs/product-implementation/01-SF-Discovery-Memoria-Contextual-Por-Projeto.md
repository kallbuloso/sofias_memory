# Sofias Memory — Discovery: Memória Contextual Persistente por Projeto

**Document:** `01-SF-Discovery-Memoria-Contextual-Por-Projeto.md`\
**Status:** APPROVED\
**Date:** 2026-11-17\
**Project:** Sofias Memory\
**Phase completed:** Discovery\
**Current role:** Product Engineer\

---

## 1. Contexto

O Sofias Memory nasceu como uma infraestrutura de memória semântica e relacional independente para aplicações, automações e agentes.

O produto já possui uma base consolidada para:

- Knowledge Memory baseada em `Dataset`, `Source`, `Document`, `Chunk`, entidades e relações;
- recuperação vetorial, lexical, híbrida e baseada em grafo;
- PostgreSQL + pgvector como fonte autoritativa;
- Neo4j como projeção reconstruível;
- Sessions persistentes;
- `SessionEntry` append-only;
- Skills;
- Agent Profiles;
- Native Cognitive Memory;
- proveniência;
- verdade temporal;
- `Supersede`;
- `Forget`;
- idempotência;
- pipelines duráveis;
- feedback sobre Knowledge Recall.

Referências de baseline existentes:

- `docs/product/Sofias_Memory_PRD_SPECS.md`
- `docs/adr/0012-first-class-durable-sessions.md`
- `docs/adr/0016-native-cognitive-memory-model-and-lifecycle.md`
- `docs/product/Sofias_Memory_Feature_Contract_v0.7.0_Native_Cognitive_Memory.md`
- `docs/product/Sofias_Memory_Integration_Contract_Sofias_Assistant_v1.md`

A versão v0.7.0 introduziu Native Cognitive Memory com `MemoryItem` dos tipos:

```text
profile
semantic
```

e os scopes:

```text
global
project:<key>
```

Essa implementação resolve adequadamente a persistência de fatos, preferências, decisões e informações semânticas duráveis.

Entretanto, a análise do uso do Sofias Memory como infraestrutura de memória de assistentes conversacionais identificou uma necessidade diferente e ainda não atendida integralmente:

> preservar a continuidade contextual de um projeto entre diferentes sessões, conversas, reinicializações e períodos de tempo.

O problema não é apenas lembrar fatos.

É permitir que uma aplicação consiga recuperar:

- o que aconteceu;
- por que determinada decisão foi tomada;
- quais alternativas foram consideradas;
- quais hipóteses foram rejeitadas;
- o que mudou;
- qual é o estado atual;
- onde o trabalho foi interrompido;
- qual seria a continuação natural.

---

## 2. Problema

A arquitetura atual do Sofias Memory possui os elementos necessários para armazenar conhecimento e contexto, mas eles permanecem semanticamente separados.

Uma `Session` preserva contexto temporal.

Uma `SessionEntry` preserva acontecimentos ordenados dentro da sessão.

Um `MemoryItem` preserva memória cognitiva durável.

Knowledge Memory preserva documentos e conhecimento baseado em fontes.

Esses recursos, porém, não formam atualmente uma representação persistente da **experiência acumulada de um projeto**.

### 2.1 Exemplo

Considere uma discussão de arquitetura na qual:

1. são avaliadas três alternativas;
2. uma delas é inicialmente preferida;
3. novos problemas são descobertos;
4. a alternativa é abandonada;
5. outra solução é aprovada;
6. ficam duas pendências para a próxima sessão.

A Native Cognitive Memory pode armazenar:

```text
O projeto utiliza a solução B.
```

A Session pode preservar toda a conversa.

Mas uma nova sessão ainda precisa descobrir:

```text
por que B foi escolhida;
por que A foi abandonada;
qual problema causou a mudança;
quais decisões são históricas;
quais decisões continuam vigentes;
o que ainda falta fazer.
```

Recarregar todas as mensagens anteriores não é uma solução escalável.

Armazenar apenas fatos finais também elimina contexto importante.

---

## 3. Formulação do problema central

O Sofias Memory ainda não possui um modelo first-class para representar e recuperar, de maneira compacta e auditável:

> **experiências relevantes, evolução de decisões e estado de continuidade pertencentes a um contexto duradouro, como um projeto.**

Isso limita sua capacidade de funcionar como infraestrutura de memória de longo prazo para assistentes que trabalham continuamente sobre os mesmos assuntos.

---

## 4. Motivação

Um assistente persistente não deve depender exclusivamente da janela de contexto do LLM.

Também não deve exigir que o usuário recapitule manualmente discussões anteriores.

Uma memória de projeto adequada deve permitir interações como:

```text
Onde paramos?

Por que escolhemos PostgreSQL?

Não tínhamos considerado outra arquitetura?

O que ainda falta implementar?

Quando mudamos essa decisão?

Qual era a solução anterior?

O que descobrimos durante aquele problema?

Continue de onde paramos.
```

Essas perguntas exigem mais do que similarity search sobre fatos individuais.

Elas dependem de temporalidade, causalidade, proveniência e estado.

---

## 5. Atores e consumidores

O Sofias Memory continua sendo infraestrutura independente.

Os principais consumidores da capability proposta são:

### 5.1 Assistentes conversacionais

Exemplos:

- Sofias Personal Assistant;
- Sofia's Assistant;
- outros assistentes pessoais ou corporativos.

Necessitam recuperar contexto persistente entre diferentes conversas.

### 5.2 Agentes

Agentes podem utilizar memória de experiências anteriores para evitar repetição de investigação ou trabalho.

Podem armazenar resultados relevantes de uma execução, mas o Sofias Memory não executa o agente.

### 5.3 Aplicações

Aplicações tradicionais podem utilizar memória contextual para:

- projetos;
- atendimentos;
- pesquisas;
- investigações;
- casos;
- workflows de longa duração.

### 5.4 Automations

Ferramentas como n8n podem utilizar o Sofias Memory como armazenamento contextual entre execuções independentes.

---

## 6. Workflow atual

O comportamento atualmente possível pode ser representado como:

```text
Conversation
    ↓
Session
    ↓
SessionEntry[]
```

Quando uma informação merece persistência cognitiva:

```text
Application
    ↓
MemoryCandidate / decisão externa
    ↓
POST /memories
    ↓
MemoryItem
```

Knowledge Memory segue outro caminho:

```text
Source
    ↓
Remember / Cognify
    ↓
Chunks / Entities / Relations / Summaries
    ↓
Recall
```

Esses planos são deliberadamente separados.

Essa separação é considerada correta e deve ser preservada.

---

## 7. Limitação do workflow atual

Não existe atualmente um caminho first-class equivalente a:

```text
Session history
      ↓
relevant experience
      ↓
persistent episode
      ↓
project evolution
      ↓
future contextual recall
```

Também não existe uma representação dedicada para:

```text
"onde este trabalho parou?"
```

Uma aplicação pode armazenar essa informação como `MemoryItem`, `SessionEntry` ou metadata, mas cada opção introduz ambiguidade semântica.

---

## 8. Oportunidade identificada

A oportunidade é introduzir uma camada de **continuity memory** sem desfazer os limites arquiteturais atuais.

Essa camada deverá permitir que o Sofias Memory represente:

```text
Knowledge
    +
Cognitive facts
    +
Temporal sessions
    +
Experiences
    +
Continuation state
```

sem transformar esses conceitos em um único objeto genérico.

---

## 9. Conceito: Project Memory

Durante o Discovery foi estabelecido o conceito de **Project Memory**.

Project Memory não implica, neste momento, uma nova entidade física no banco.

Representa:

> o conjunto de memórias, experiências, sessões, conhecimento e estado contextual relacionados a um contexto duradouro identificado por um projeto.

Exemplo:

```text
project:sofias-personal-assistant
```

Esse scope já é válido na Native Cognitive Memory.

A evolução deverá permitir que diferentes planos de memória utilizem esse mesmo contexto lógico sem obrigatoriamente compartilhar o mesmo modelo físico.

---

## 10. Project Memory vs Project Context

Foi aprovada uma distinção conceitual central.

### Project Memory

Representa tudo que foi preservado sobre o projeto.

Pode incluir:

- fatos;
- decisões;
- documentos;
- sessões;
- experiências;
- histórico;
- conhecimento relacionado.

É potencialmente grande e cresce com o tempo.

### Project Context

Representa somente a parcela necessária para compreender a situação atual ou responder à consulta corrente.

Exemplo:

```text
Project Memory
≈ milhares de registros

Project Context
≈ poucas evidências relevantes
```

Essa separação é essencial para evitar que crescimento da memória implique crescimento ilimitado do prompt do LLM.

---

## 11. Episode

Fica aprovada a direção de introduzir uma representação first-class de experiência relevante denominada:

```text
Episode
```

Um Episode poderá representar:

- decisão;
- investigação;
- experimento;
- incidente;
- mudança de direção;
- descoberta;
- resultado;
- aprendizado;
- marco relevante.

Exemplo conceitual:

```text
Episode

Title:
Escolha da estratégia de recuperação contextual

Situation:
Foram avaliadas recuperação puramente vetorial e recuperação híbrida.

Decision:
Foi selecionada a abordagem híbrida para experimentação.

Reason:
Similarity search isolada não preservava adequadamente a sequência de decisões.

Outcome:
Estratégia aceita para o próximo experimento.

Evidence:
Session/SessionEntry references.
```

### 11.1 Episode não é um fato vigente

Uma experiência histórica permanece verdadeira como acontecimento mesmo quando sua conclusão deixa de representar o estado atual.

Exemplo:

```text
"Em agosto decidimos utilizar arquitetura A."
```

continua sendo historicamente verdadeiro mesmo se:

```text
"Em outubro substituímos A por B."
```

Isso diferencia `Episode` de `MemoryItem.semantic`.

### 11.2 Episode será agregado first-class

`Episode` será modelado separadamente de `MemoryItem`.

O contrato atual de Cognitive Memory permanece:

```text
MemoryItem = profile | semantic
```

Não será introduzido `memory_type=episodic` como parte desta evolução.

---

## 12. Context Checkpoint

Fica aprovada a criação futura de uma representação first-class para:

> o estado em que uma atividade ou contexto duradouro se encontra e de onde pode ser retomado.

Nome de trabalho aprovado:

```text
ContextCheckpoint
```

Um checkpoint poderá representar:

```text
objective
current_state
approved_decisions
open_items
blockers
next_action
evidence references
```

Exemplo:

```text
Objective:
Evoluir recuperação contextual do Sofias Memory.

Current state:
Arquitetura conceitual em análise.

Approved:
PostgreSQL permanece autoritativo.

Open:
Contrato de memória episódica.

Next action:
Revisar o ADR proposto.
```

O checkpoint não substitui Sessions, Tasks ou sistemas externos de gerenciamento de projetos.

Sua função é permitir **retomada contextual**.

### 12.1 Ownership

`ContextCheckpoint` pertence ao contexto duradouro identificado pelo `scope`, e não a uma única Session.

Exemplo:

```text
scope = project:sofias-memory
```

Sessions e SessionEntries podem ser referenciadas como evidências do checkpoint.

---

## 13. Modelo de continuidade

A combinação conceitual aprovada é:

```text
MemoryItem
    ↓
o que sabemos

Episode
    ↓
o que aconteceu e por quê

ContextCheckpoint
    ↓
onde estamos agora

Knowledge Memory
    ↓
quais fontes sustentam o conhecimento

Session
    ↓
qual foi o histórico temporal original
```

Uma recuperação contextual poderá utilizar esses planos conjuntamente sem fundi-los fisicamente.

---

## 14. Contexto global e contexto de projeto

A Native Cognitive Memory já distingue:

```text
global
project:<key>
```

A futura recuperação contextual deverá preservar explicitamente essa separação.

Exemplo:

```text
global
    preferred language
    general user preferences

project:sofias-memory
    architecture decisions
    project facts
    project episodes
    checkpoints

project:sofias-lyder
    independent project memory
```

Memórias semanticamente semelhantes em projetos diferentes não devem ser automaticamente fundidas.

---

## 15. Comparação estudada

Durante Discovery foram utilizados Cognee e OpenJarvis como referências funcionais.

### 15.1 Cognee

Aspectos considerados relevantes:

- memory enrichment;
- session distillation;
- graph relationships;
- hybrid retrieval;
- conflict review;
- feedback;
- context-oriented retrieval.

A principal contribuição conceitual é:

> memória pode ser continuamente enriquecida e consolidada sem permanecer apenas como coleção de embeddings independentes.

### 15.2 OpenJarvis

Aspectos considerados relevantes:

- persistência de estado;
- agent traces;
- observation compression;
- memory extraction strategies;
- recuperação de experiências;
- retomada de execução.

A principal contribuição conceitual é:

> experiências de execução também podem gerar contexto reutilizável no futuro.

### 15.3 Regra de adoção

Cognee e OpenJarvis são referências.

O objetivo não é reproduzir suas arquiteturas integralmente.

Qualquer capability incorporada ao Sofias Memory deverá respeitar suas decisões atuais de autoridade, simplicidade e independência.

---

## 16. Restrições conhecidas

As seguintes restrições fazem parte do baseline atual e permanecem válidas.

### 16.1 PostgreSQL continua autoritativo

Dados duráveis e lifecycle pertencem ao PostgreSQL.

Neo4j permanece projeção reconstruível quando houver necessidade de representação em grafo.

### 16.2 Sofias Memory não é Agent Runtime

Não assume responsabilidade por:

- planejamento;
- execução de tools;
- scheduler de agentes;
- autorização de ações;
- provider sessions;
- prompt orchestration;
- runtime de tarefas.

### 16.3 Memory não representa autoridade de execução

Conteúdo recuperado é contexto.

Não pode se transformar implicitamente em:

- system instruction;
- PermissionGrant;
- Policy;
- autorização;
- comando privilegiado.

### 16.4 Sessions permanecem contexto temporal

`SessionEntry` não se torna automaticamente memória permanente.

### 16.5 Cognitive Memory permanece explícita

O baseline v0.7.0 estabelece:

```text
candidate != memory
```

A promoção de uma informação para memória cognitiva permanente continua dependendo de decisão explícita do caller.

### 16.6 Compatibilidade

A evolução deverá ser preferencialmente aditiva.

Consumidores da API existente não podem mudar de comportamento silenciosamente.

---

## 17. Dores identificadas

### D-01 — Retomada depende demais do histórico original

Uma nova sessão precisa reconstruir contexto procurando em conversas anteriores.

### D-02 — Similarity search não representa causalidade

Uma pergunta sobre "por que" pode exigir múltiplas evidências relacionadas.

### D-03 — Estado atual e experiência histórica são conceitos diferentes

O modelo atual não possui agregados dedicados para essa diferença.

### D-04 — Decisões antigas podem continuar semanticamente similares às atuais

Similarity search pode recuperar uma decisão obsoleta sem contexto suficiente.

### D-05 — Sessões longas não escalam como contexto integral

Inserir todo o histórico em um LLM é caro e produz ruído.

### D-06 — Informações de continuidade podem acabar escondidas em metadata

Isso cria comportamento dependente de convenções do caller.

### D-07 — Não existe recuperação unificada entre os planos de memória

Knowledge Recall e Cognitive Recall são APIs distintas e Sessions possuem semântica própria.

---

## 18. Oportunidades

### O-01 — Retomada natural entre sessões

Permitir:

```text
Continue de onde paramos.
```

sem depender de histórico reenviado pelo usuário.

### O-02 — Memória causal

Responder:

```text
Por que tomamos essa decisão?
```

com evidências relacionadas.

### O-03 — Memória histórica

Responder:

```text
O que decidimos antes?
```

sem confundir com a decisão vigente.

### O-04 — Memória de experiências de agentes

Permitir que resultados relevantes de execuções externas sejam reutilizados.

### O-05 — Recuperação contextual composta

Combinar diferentes evidências sob uma consulta contextual.

### O-06 — Redução de contexto enviado ao LLM

Reconstruir somente a informação relevante.

---

## 19. Hipóteses a validar durante as próximas fases

As seguintes afirmações continuam como hipóteses técnicas ou de produto e deverão ser validadas durante Specs, Architecture ou Implementation.

### H-01

Um agregado first-class `Episode` deverá representar experiências com menor ambiguidade do que uma extensão de `MemoryItem`.

A direção arquitetural foi aprovada; a eficácia deverá ser validada.

### H-02

`ContextCheckpoint` poderá resolver grande parte do problema de retomada antes da implementação de mecanismos avançados de consolidação.

### H-03

A recuperação contextual deverá combinar múltiplos tipos de evidência em vez de depender exclusivamente de cosine similarity.

### H-04

O scope `project:<key>` existente poderá funcionar inicialmente como principal identidade lógica de Project Memory.

### H-05

Uma entidade formal `MemorySpace` poderá ser útil futuramente, mas não é necessária para o primeiro incremento.

### H-06

Extração e consolidação automáticas deverão ser introduzidas somente depois que uma versão supervisionada estiver validada.

---

## 20. Decisões herdadas do baseline

### DEC-01

PostgreSQL + pgvector é fonte autoritativa.

### DEC-02

Neo4j é projeção reconstruível.

### DEC-03

Session é contexto temporal persistente.

### DEC-04

SessionEntry é append-only e não representa memória cognitiva permanente.

### DEC-05

Native Cognitive Memory é um domínio separado.

### DEC-06

Native Cognitive Memory v0.7.0 possui `profile` e `semantic`.

### DEC-07

Cognitive scopes incluem:

```text
global
project:<key>
```

### DEC-08

Supersession é explícita.

### DEC-09

Forget cognitivo é destrutivo sobre o conteúdo do `MemoryItem` alvo.

### DEC-10

Sofias Memory não executa agents ou skills.

### DEC-11

Contexto recuperado não cria autoridade operacional.

---

## 21. Decisões aprovadas neste Discovery

### DEC-12 — Project Memory

Project Memory será tratado como contexto lógico duradouro capaz de reunir diferentes planos de memória relacionados ao mesmo projeto.

Não exige, neste momento, nova entidade física.

### DEC-13 — Project Memory e Project Context são conceitos distintos

Project Memory representa o conjunto completo de informação preservada.

Project Context representa a seleção necessária para compreender ou continuar uma atividade específica.

### DEC-14 — Episode será first-class

`Episode` será agregado próprio e separado de `MemoryItem`.

O contrato `MemoryItem = profile | semantic` permanece intacto.

### DEC-15 — Experience != current truth

Episode representa acontecimento histórico.

Não é automaticamente considerado verdade vigente.

### DEC-16 — ContextCheckpoint será first-class

Será criada uma representação dedicada de continuidade contextual.

### DEC-17 — Checkpoint pertence ao contexto duradouro

`ContextCheckpoint` pertence ao contexto identificado pelo `scope`.

Uma Session pode ser evidência, mas não é owner exclusivo do checkpoint.

### DEC-18 — Recuperação contextual será composta

A evolução deverá permitir recuperação coordenada entre múltiplos planos de memória.

As APIs atuais não serão silenciosamente substituídas.

### DEC-19 — Orchestration permanece externa

Sofias Memory recupera e disponibiliza contexto.

A composição final do contexto entregue ao LLM permanece responsabilidade do caller/ContextBuilder.

### DEC-20 — Supervised-first

A primeira implementação de Episodes e Checkpoints será explícita/supervisionada.

Extração ou consolidação automática não faz parte do primeiro incremento.

---

## 22. Resoluções das Open Questions

As questões anteriormente classificadas como `NEEDS DECISION NOW` estão encerradas.

### Q-01 — Episode deve ser first-class?

**DECIDED: SIM.**

`Episode` será agregado first-class separado de `MemoryItem`.

Razões:

- experiência histórica possui semântica distinta de verdade vigente;
- um acontecimento não deixa de ter ocorrido após mudança de decisão;
- `MemoryItem` mantém lifecycle adequado a facts/preferences/current semantic truth;
- preserva compatibilidade da Native Cognitive Memory v0.7.0;
- evita forçar `Supersede` a representar história episódica.

Regra:

```text
MemoryItem = profile | semantic
Episode    = historical experience
```

---

### Q-02 — Checkpoint pertence à Session ou ao Project Context?

**DECIDED: PROJECT CONTEXT / DURABLE SCOPE.**

O checkpoint pertence ao contexto duradouro identificado pelo scope.

Exemplo:

```text
project:sofias-memory
    ├── Session A
    ├── Session B
    ├── Session C
    └── ContextCheckpoint revisions
```

Sessions podem ser evidência:

```text
source_session_uuid
source_entry_ids
evidence_refs
```

mas não definem a identidade do checkpoint.

Uma entidade `MemorySpace` não é necessária neste momento.

---

### Q-03 — O que determina o checkpoint current?

**DECIDED: LINEAR EXPLICIT SUPERSESSION.**

Não será utilizado apenas `newest wins`.

Um checkpoint deverá possuir conceitualmente:

```text
checkpoint_id
scope
revision
lifecycle
superseded_by
created_at
```

Lifecycle inicial:

```text
active
superseded
```

Invariante:

> Para um mesmo contexto lógico, existe no máximo um `ContextCheckpoint ACTIVE`.

A criação de nova revisão deverá realizar atomicamente:

```text
current ACTIVE -> SUPERSEDED
new checkpoint -> ACTIVE
```

Também deverá existir proteção de concorrência baseada no checkpoint esperado.

Conceitualmente:

```text
expected_current_checkpoint_id
```

Quando o estado esperado não corresponder ao estado atual:

```text
409 CONTEXT_STATE_CONFLICT
```

Os detalhes finais do Contract serão definidos posteriormente.

---

### Q-04 — Como Forget afeta Episodes e Checkpoints derivados?

**DECIDED: PROVENANCE-AWARE INVALIDATION.**

Forget não pode ser contornado pela existência de paráfrases ou derivados persistidos em outros planos de memória.

Regra conceitual:

```text
derived contextual record
        ↓
evidence references
        ↓
evidence availability
```

Quando uma evidência é esquecida:

1. sua referência deixa de disponibilizar conteúdo;
2. o derivado é reavaliado segundo suas demais evidências;
3. se não existir evidência independente suficiente, o conteúdo derivado deve ser scrubbed ou invalidado;
4. se existirem evidências independentes suficientes, o derivado pode permanecer, com provenance atualizada.

Exemplo:

```text
Episode E1
evidence:
  - Memory A
  - SessionEntry B
  - Document C
```

Se `Memory A` for esquecida e B + C sustentarem independentemente E1:

```text
E1 pode permanecer.
```

Por outro lado:

```text
Episode E2
evidence:
  - Memory A
```

Se A for esquecida:

```text
E2 não pode preservar uma paráfrase recuperável de A.
```

Não será adotado cascade delete cego.

A política será baseada em proveniência, disponibilidade e suporte independente.

Os detalhes normativos de scrub, invalidation e lifecycle serão definidos em Spec/Contract próprios.

---

## 23. Regras derivadas aprovadas

### RULE-01 — Episode não representa automaticamente verdade vigente

Conteúdo episódico pode informar uma consulta histórica ou causal, mas não substitui `MemoryItem.semantic` como representação de truth/current semantic state.

### RULE-02 — ContextCheckpoint não é autoridade operacional

Um checkpoint pode registrar:

```text
next_action = "Implementar SM-CTX-004"
```

mas isso não transforma o Sofias Memory em task manager, scheduler ou Agent Runtime.

A autoridade real sobre tarefas externas continua pertencendo ao sistema que as gerencia.

### RULE-03 — Provenance faz parte da semântica contextual

Episodes e Checkpoints derivados deverão manter referências suficientes para explicar de onde suas informações vieram e para reagir corretamente quando evidências deixarem de estar disponíveis.

---

## 24. Deferred

### DQ-01 — MemorySpace first-class

Uma entidade `MemorySpace` poderá organizar projetos futuramente.

Não é necessária para validar continuidade.

### DQ-02 — Projeção de Episodes no Neo4j

Só deverá ser considerada se graph retrieval provar valor mensurável.

### DQ-03 — Automatic distillation

Não faz parte do primeiro incremento.

### DQ-04 — Contradiction resolution por LLM

Poderá ser avaliada depois que detecção e supervised reconciliation estiverem definidas.

### DQ-05 — Importance score

Não deverá ser criado antes de existir política explícita para seu uso.

### DQ-06 — Multi-user / tenancy

Continua fora do objetivo atual.

---

## 25. Fora de escopo

Não fazem parte desta evolução:

- Agent Runtime;
- tool execution;
- scheduler;
- multi-agent orchestration;
- generic workflow engine;
- multi-user SaaS;
- RBAC genérico;
- ACL por projeto;
- provider routing;
- prompt management;
- autonomous decision authority;
- automatic self-modification;
- substituição do PostgreSQL como autoridade.

---

## 26. Riscos identificados

### R-01 — Overengineering

Criar muitos agregados antes de comprovar valor pode aumentar complexidade sem melhorar recuperação.

**Mitigação:** implementar verticalmente e validar continuidade real.

### R-02 — Duplicação excessiva

Episodes e Checkpoints podem copiar conteúdo já existente em Sessions e MemoryItems.

**Mitigação:** armazenar sínteses e referências de evidência, não transcrições completas.

### R-03 — Truth ambiguity

Episode histórico pode ser confundido com verdade atual.

**Mitigação:** agregados e semânticas explicitamente separados.

### R-04 — Memory pollution

Extração automática pode preservar informações irrelevantes ou incorretas.

**Mitigação:** primeira implementação supervisionada.

### R-05 — Forget incompleto

Conteúdo apagado em um plano pode sobreviver indevidamente em um derivado.

**Mitigação:** provenance-aware invalidation obrigatória.

### R-06 — Retrieval explosion

Consultar todos os planos indiscriminadamente pode aumentar latência e ruído.

**Mitigação:** recuperação seletiva, budgets e ranking composto.

### R-07 — Scope confundido com autorização

`project:<key>` é namespace contextual, não security boundary.

**Mitigação:** preservar explicitamente essa regra nos Contracts.

### R-08 — Checkpoint incorreto tornar-se current

Concorrência pode produzir dois estados aparentemente atuais.

**Mitigação:** linear supersession + optimistic concurrency + invariante de um único ACTIVE.

---

## 27. Experimento mínimo de produto

Antes de implementar a solução completa, o primeiro incremento deverá provar uma capacidade simples.

### Session A

O caller:

1. inicia trabalho em `project:test`;
2. registra contexto;
3. registra uma decisão;
4. registra uma pendência;
5. produz um checkpoint;
6. encerra a interação.

### Session B

Outra sessão, em outro processo, inicia posteriormente.

Sem receber manualmente o histórico da Session A, o consumidor deverá recuperar corretamente:

```text
qual era o objetivo;
qual decisão foi tomada;
qual pendência permaneceu;
qual é o próximo passo.
```

Em evolução posterior, um Episode deverá permitir também responder:

```text
por que essa decisão foi tomada?
```

### Critério central

O sucesso não é:

```text
"A informação existe no banco."
```

O sucesso é:

```text
"O caller consegue reconstruir corretamente o contexto de continuidade."
```

---

## 28. Discovery Exit Criteria

Os critérios de encerramento foram satisfeitos.

### Aprovado

- necessidade de Project Memory/context continuity;
- separação entre Project Memory e Project Context;
- necessidade de representação explícita de continuidade;
- `Episode` como agregado first-class;
- `ContextCheckpoint` pertencente ao durable project scope;
- single ACTIVE checkpoint com supersession explícita;
- princípio de recuperação contextual composta;
- responsabilidade do caller versus Sofias Memory;
- provenance-aware Forget sobre derivados;
- experimento mínimo de continuidade.

### Não precisa ser decidido nesta fase

- schema físico;
- endpoints finais;
- índices;
- ranking matemático;
- Neo4j projection;
- implementação de distillation automática;
- MemorySpace first-class.

**Discovery: COMPLETED.**

---
