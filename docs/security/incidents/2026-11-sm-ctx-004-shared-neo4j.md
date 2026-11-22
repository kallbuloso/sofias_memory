# Incidente: acesso ao Neo4j compartilhado durante a validação do SM-CTX-004

```text
Status:            REGISTRADO (correção histórica); estado residual NÃO verificado
Data do incidente: 2026-11-21 (UTC)
Detectado em:      2026-11-21, durante a implementação do SM-CTX-005
Registrado em:     2026-11-22 (closeout documental do SM-CTX-005)
Afeta:             closeout do SM-CTX-004 em
                   docs/product-implementation/08-SF-Technical-Backlog-Memoria-Contextual-Por-Projeto.md
Escopo:            banco de teste dedicado sofias_memory_ctx_episodes_test e a
                   instância Neo4j de desenvolvimento compartilhada
```

Este documento não contém credenciais, connection strings nem valores de
configuração. A instância compartilhada é referida apenas como "Neo4j de
desenvolvimento compartilhado" (o `NEO4J_URI` configurado no ambiente local de
desenvolvimento).

## 1. Resumo

Servidores usados na Human Validation dos Slices contextuais foram iniciados com
a factory normal da aplicação apontando para o banco de teste dedicado, mas sem
desabilitar o Neo4j. Por isso cada processo herdou o `NEO4J_URI` da configuração
local de desenvolvimento, conectou-se ao Neo4j compartilhado, executou o
bootstrap de schema e manteve ativo o consumidor autônomo de `graph_outbox`.
Um desses processos projetou 30 eventos de grafo de um dataset sintético no
Neo4j compartilhado.

**A afirmação do closeout do SM-CTX-004 de que o Neo4j compartilhado não foi
tocado estava incorreta.**

## 2. Circunstâncias

- Os servidores de validação foram iniciados com a factory normal
  (`uvicorn sofias_memory.app:create_app --factory`) e `DATABASE_URL`
  apontando para o banco de teste dedicado. Neo4j não foi desabilitado; a
  configuração local de desenvolvimento forneceu o `NEO4J_URI` da instância
  compartilhada.
- Lançamentos desse tipo (porta 8011, banco de teste) observados na
  investigação, por horário de início (UTC): 13:35, 13:36, 14:01, 14:02 (×2),
  15:29 (×2), 16:10, 16:11, 18:08 e 18:30. Todos chegaram ao estado
  `operational`.
- Um lançamento às 18:22 UTC usou um `NEO4J_URI` deliberadamente inalcançável e
  nunca saiu do bootstrap.
- O único servidor desse dia iniciado com `create_app(enable_neo4j=False)`
  (porta 8012, 18:27–18:30 UTC) executou o pipeline real de Knowledge. Ele não
  tinha recurso Neo4j nem consumidor de outbox; o step
  `projection_convergence` rodou em 0 ms e os eventos de grafo gerados ficaram
  `pending`.
- Às 18:30:39 UTC o servidor da Human Validation A–I do SM-CTX-004 foi iniciado
  como aplicação normal (porta 8011) e ficou `operational` às 18:30:41. O
  consumidor de `graph_outbox` desse processo encontrou os eventos pendentes do
  dataset sintético e os projetou no Neo4j compartilhado.

## 3. Recursos ativados pela factory normal

Em cada lançamento normal:

- recurso Neo4j criado a partir da configuração local (conexão com a instância
  compartilhada);
- probe de startup e `ensure_neo4j_schema` (DDL `CREATE CONSTRAINT … IF NOT
  EXISTS` / `CREATE INDEX … IF NOT EXISTS`) executados contra a instância
  compartilhada;
- consumidor autônomo de `graph_outbox` ativo durante toda a vida do processo.

## 4. Escritas atribuíveis (certeza)

- Dataset sintético: `hv-ctx004-k-mwqqqma1`
  (id `1299201f-54bd-44ed-b8ab-787ff074ce8f`), conteúdo sintético.
- Ids `graph_outbox` **25–54** (30 eventos), uma tentativa por evento,
  processados entre 18:30:44.767 e 18:30:49.775 UTC (`processed_at`, relógio do
  PostgreSQL):
  - 15 `upsert` (gerados pelo Remember às 18:28:53): 5 entity, 1 chunk,
    5 entity_mention, 4 relation;
  - 15 `delete` (gerados pelo Forget da Source às 18:30:00) sobre exatamente os
    mesmos aggregate ids.

| id | aggregate | aggregate_id | operação | processed_at (UTC) |
|---|---|---|---|---|
| 25 | entity | `a2a5cff4-13fb-4a27-8917-b355b030d803` | upsert | 18:30:44.767 |
| 26 | entity | `ad528953-1fbf-457f-bf74-dc4443346bca` | upsert | 18:30:45.579 |
| 27 | entity | `3f88ff26-5311-44bc-9e9e-1b9221e563e3` | upsert | 18:30:45.799 |
| 28 | entity | `445ea43a-8eb0-4d32-a570-4660579a4c0b` | upsert | 18:30:45.852 |
| 29 | entity | `37843667-80d2-440c-b5cb-3790fafa901d` | upsert | 18:30:45.912 |
| 30 | chunk | `5c5a8e7e-464a-5529-8de0-be1f68d90cbc` | upsert | 18:30:46.098 |
| 31 | entity_mention | `b52c33ff-674a-4c5b-8a41-aa0a0901b8c0` | upsert | 18:30:47.083 |
| 32 | entity_mention | `0238b9b2-7c0a-4ada-acfd-eb4828d709d9` | upsert | 18:30:47.134 |
| 33 | entity_mention | `4726e523-74c3-490e-a9b2-13402ee0f26f` | upsert | 18:30:47.181 |
| 34 | entity_mention | `38a355d0-df93-459e-9236-8757255d011a` | upsert | 18:30:47.233 |
| 35 | entity_mention | `0eb01e0b-5e5a-4b36-9b09-0b29a05f61d6` | upsert | 18:30:47.284 |
| 36 | relation | `56fea082-d4e2-46b6-95a2-b77bcbfc065a` | upsert | 18:30:47.634 |
| 37 | relation | `d3e068e9-5b96-4a13-9a17-11bb53190217` | upsert | 18:30:47.685 |
| 38 | relation | `965e07c1-2f47-45f3-9f89-4411cd00ab28` | upsert | 18:30:47.989 |
| 39 | relation | `c93941cf-dd90-4539-8da9-b9dac775192a` | upsert | 18:30:48.039 |
| 40 | chunk | `5c5a8e7e-464a-5529-8de0-be1f68d90cbc` | delete | 18:30:48.625 |
| 41 | entity_mention | `0238b9b2-7c0a-4ada-acfd-eb4828d709d9` | delete | 18:30:48.957 |
| 42 | entity_mention | `0eb01e0b-5e5a-4b36-9b09-0b29a05f61d6` | delete | 18:30:49.000 |
| 43 | entity_mention | `38a355d0-df93-459e-9236-8757255d011a` | delete | 18:30:49.041 |
| 44 | entity_mention | `4726e523-74c3-490e-a9b2-13402ee0f26f` | delete | 18:30:49.088 |
| 45 | entity_mention | `b52c33ff-674a-4c5b-8a41-aa0a0901b8c0` | delete | 18:30:49.144 |
| 46 | relation | `56fea082-d4e2-46b6-95a2-b77bcbfc065a` | delete | 18:30:49.350 |
| 47 | relation | `965e07c1-2f47-45f3-9f89-4411cd00ab28` | delete | 18:30:49.398 |
| 48 | relation | `c93941cf-dd90-4539-8da9-b9dac775192a` | delete | 18:30:49.445 |
| 49 | relation | `d3e068e9-5b96-4a13-9a17-11bb53190217` | delete | 18:30:49.491 |
| 50 | entity | `37843667-80d2-440c-b5cb-3790fafa901d` | delete | 18:30:49.593 |
| 51 | entity | `3f88ff26-5311-44bc-9e9e-1b9221e563e3` | delete | 18:30:49.636 |
| 52 | entity | `445ea43a-8eb0-4d32-a570-4660579a4c0b` | delete | 18:30:49.682 |
| 53 | entity | `a2a5cff4-13fb-4a27-8917-b355b030d803` | delete | 18:30:49.729 |
| 54 | entity | `ad528953-1fbf-457f-bf74-dc4443346bca` | delete | 18:30:49.774 |

## 5. Escritas prováveis sem atribuição

- DDL idempotente de schema executado por cada um dos lançamentos normais.
- As linhas `graph_outbox` 1–24 não existem mais no banco de teste. Não é
  possível determinar se algum desses eventos foi projetado por um dos
  processos acima.

## 6. Estado residual — incerteza explícita

- A sequência upsert → delete sobre os mesmos ids sugere efeito líquido nulo
  para os nós e relações do dataset `1299201f-…`. **Isso não foi verificado.**
  Nenhuma consulta foi feita ao Neo4j compartilhado (nem de leitura), por
  decisão deliberada.
- Resíduos não descartados: nós ou relacionamentos auxiliares criados pelas
  projeções, propriedades/labels não removidos pelos deletes, efeitos de
  eventos 1–24, constraints/índices criados caso ainda não existissem.
- **A instância compartilhada foi modificada. Este registro não afirma que ela
  está limpa.**

## 7. Afirmações corrigidas

No closeout do SM-CTX-004 (Technical Backlog contextual):

- "Neo4j compartilhado não foi tocado" — **incorreta**;
- "15 eventos de grafo `pending`" — **incorreta**: os eventos foram processados
  pelo servidor da Human Validation às 18:30 UTC.

Continuam corretas as afirmações de que o pipeline de Knowledge e o Recall
validados rodaram com Neo4j desabilitado e de que a projeção/reconciliação Neo4j
**não** foi validada no SM-CTX-004. Os relatórios de validação, commit e
closeout do SM-CTX-004 repetiram a afirmação incorreta; a correção no backlog
foi registrada no closeout documental do SM-CTX-005.

## 8. Medidas preventivas adotadas (desde o SM-CTX-005)

- Servidores de validação sobem apenas por um lançador explícito que chama
  `create_app(enable_neo4j=False)` e aborta antes de servir se existir recurso
  Neo4j (`app.state.neo4j_resource`) ou consumidor de outbox no worker.
- Proibido iniciar a factory normal contra bancos de teste; sobrescrever apenas
  `NEO4J_URI` não é o padrão aprovado.
- Antes de iniciar qualquer servidor de validação, verificar linhas pendentes em
  `graph_outbox`.
- As suítes de integração usam `create_app(..., enable_neo4j=False)` com
  `graph_outbox_processor=None`.
- A Human Validation A–J do SM-CTX-005 usou somente o lançador protegido; os 87
  eventos de Knowledge gerados permaneceram `pending`.

## 9. Remediação restante

- Verificação do estado residual na instância compartilhada, a cargo do
  responsável por ela e fora das tarefas de implementação: procurar nós e
  relações com `dataset_id = '1299201f-54bd-44ed-b8ab-787ff074ce8f'` e com os
  aggregate ids listados na seção 4, além de avaliar efeitos de eventos 1–24.
  Nenhum agente de implementação deve acessar ou reconciliar a instância sem
  autorização explícita.
- Validação real de projeção/reconciliação Neo4j dos fluxos contextuais
  continua NÃO verificada e deve usar uma instância Neo4j dedicada e descartável.
