# Sofias Memory — Feature Contract v0.6.0: Automatic Serialized Migration Bootstrap

**Status:** Proposed\
**Target release:** v0.6.0\
**Feature:** Automatic Serialized Migration Bootstrap\
**Architecture authority:** ADR-0015 (`docs/adr/0015-automatic-serialized-migration-bootstrap.md`, accepted)

---

## 1. Objetivo

O v0.6.0 elimina a necessidade operacional de um passo manual `alembic upgrade head` no caminho comum de upgrade, sem enfraquecer nenhuma das propriedades de segurança que o contrato de migration explícita (ADR-0011 D31/D32) existia para proteger: fail-closed startup, PostgreSQL como fonte única de verdade do schema, segurança de concorrência, segurança de rollback e portabilidade entre os deployment targets efetivamente suportados por este projeto (Docker Compose, Portainer, EasyPanel).

> Sofias Memory ganha um **automatic, serialized, fail-closed migration bootstrap**, integrado à sequência de startup já existente (`lifespan.py`), como substituto operacional — nunca conceitual — do passo manual de hoje.

Este feature não é:

- remoção do Alembic como autoridade de schema;
- ocultação de falha de migration;
- schema evolution implícita;
- distributed lock service;
- migration audit table;
- dedicated migration database role;
- rolling-upgrade support;
- repair/inference automático de schema não versionado.

ADR-0015 é a autoridade arquitetural para toda decisão abaixo. Este contrato não reabre nenhuma decisão do ADR; ele a formaliza em termos de comportamento público observável e de execução.

---

## 2. Princípios

- PostgreSQL permanece a única fonte de verdade do schema — `alembic_version`, sempre re-lido, nunca inferido ou cacheado entre tentativas;
- Alembic permanece a única autoridade de evolução de schema — o bootstrap invoca exatamente o mesmo `alembic upgrade head` que um operador já executa manualmente hoje, nunca uma reimplementação;
- fail-closed é o padrão em qualquer estado ambíguo — a ambiguidade nunca é resolvida por inferência automática (`stamp`, `downgrade`, ou heurística de "provavelmente é isso");
- a arquitetura escolhida (FastAPI lifespan) preserva o `/health/live` guarantee (ADR-0011 D33) durante toda a duração do bootstrap, incluindo migrations longas;
- o subprocess Alembic roda com o mesmo `DATABASE_URL`/role já usado em runtime — este feature não é, e não afirma ser, um mecanismo de privilege separation;
- nenhuma superfície pública nova é introduzida — este é um feature inteiramente interno ao processo de startup.

---

# 3. Migration mode — `DATABASE_MIGRATION_MODE`

Uma única variável de configuração controla o comportamento do bootstrap.

A spelling `DATABASE_MIGRATION_MODE` foi verificada contra `sofias_memory/config.py` neste ciclo de trabalho: não há conflito com nenhum alias `Field(...)` existente na classe `Settings`, e o padrão `Literal[...]` já estabelecido por `storage_backend: Literal["filesystem", "s3"] = Field(default="filesystem", alias="STORAGE_BACKEND")` é o template direto a ser reutilizado. A spelling está **congelada** por este contrato, não apenas sugerida.

```python
database_migration_mode: Literal["auto", "verify_only"] = Field(
    default="auto",
    alias="DATABASE_MIGRATION_MODE",
)
```

## 3.1 `auto` (default)

O bootstrap pode migrar um banco cujo revision corrente seja um ancestral genuíno do head da aplicação — incluindo pristine fresh schema (§4.1), nunca um unversioned-non-empty schema (§4.2, sempre fail-closed) — para exatamente esse head, sob a disciplina de lock e verificação definida abaixo (§5–§9).

## 3.2 `verify_only`

Reproduz exatamente o contrato pré-ADR-0015: o bootstrap executa o mesmo check read-only já existente hoje, **nunca invoca Alembic sob nenhuma circunstância**, e permanece fail-closed em `BOOTSTRAP_MAINTENANCE` para qualquer estado que não seja exact head match.

---

# 4. Schema classification — Alembic DAG é a autoridade

| Estado | `auto` | `verify_only` |
|---|---|---|
| **Pristine fresh schema** (§4.1) | migra para head | permanece not-ready |
| **Unversioned non-empty schema** (§4.2) | **fail closed** | **fail closed** |
| Exact application head | no-op | no-op |
| Ancestral único conhecido do head | migra para head | permanece not-ready |
| Múltiplos heads no código Alembic da aplicação | fail closed | fail closed |
| Múltiplos revisions registrados no banco | fail closed | fail closed |
| Revision desconhecido/não reconhecido | fail closed | fail closed |
| Revision divergente (não ancestral do head) | fail closed | fail closed |
| Revision do banco mais novo que o head da aplicação (rollback scenario) | fail closed — nunca `downgrade`/`stamp`/repair automático | fail closed |

## 4.1 Pristine fresh schema

Definição exata, nunca aproximada: `alembic_version` **ausente** **e** nenhuma application base table existe no schema atual da aplicação. Extensões, tipos ou funções fora do escopo de tabela de aplicação não tornam um banco não-pristine — apenas application base tables contam.

Pristine fresh schema é um estado **distinto e mais estreito** que "vazio/unversioned" em geral, e os dois nunca são colapsados em uma única categoria auto-migrável. A ausência de `alembic_version` sozinha não prova instalação genuinamente nova — pode significar metadata de migration danificada, schema legado/parcial, restore incompleto, ou schema estranho/não gerenciado que apenas não tem essa tabela de bookkeeping.

## 4.2 Unversioned non-empty schema — sempre fail-closed

`alembic_version` ausente **e** uma ou mais application base tables já existem. **Sempre fail closed, em ambos os modos.** O bootstrap nunca executa `alembic stamp`, nunca infere revision a partir de quais tabelas existem, e nunca tenta qualquer outro "repair" automático. Resolução é exclusivamente manual: inspeção do schema, `stamp` deliberado quando genuinamente correto, ou restore de backup conhecido — exatamente como hoje.

## 4.3 Ancestralidade — autoridade

Determinada exclusivamente pela API oficial de revision-graph do Alembic (`alembic.script.ScriptDirectory` — `get_heads()`, `get_revision()`/`get_revisions()`, `iterate_revisions()` ou equivalente da versão instalada), **nunca** por parsing manual de nomes de arquivo ou strings `down_revision`. Migration nunca executa antes da classificação estar completa.

---

# 5. Advisory lock contract

- **Tipo:** session-level (`pg_advisory_lock`/`pg_try_advisory_lock`/`pg_advisory_unlock`) — nunca transaction-level (`pg_advisory_xact_lock`); o lock deve sobreviver à invocação inteira do subprocess Alembic e ser liberado explicitamente, independente de qualquer commit/rollback de transação.
- **Key:** uma única constante fixa e documentada (bigint 63-bit-safe), escopada a "Sofias Memory schema migration bootstrap." Nenhuma key derivada de hash de runtime; nenhuma forma de dois inteiros — este é um produto single-tenant, uma database por instância.
- **Ownership:** o lock é adquirido e mantido em uma conexão **dedicada ao bootstrap**, nunca a mesma conexão/pool usada para queries de negócio ou para o readiness check pré-existente. O subprocess Alembic abre suas próprias conexões independentes (`migrations/env.py`, inalterado) — seguro porque advisory locks do PostgreSQL serializam por key, server-side, através de todas as sessões.
- **Explicitamente não usado:** tabela `migration_locks`, key derivada de `hash()` Python, ou qualquer lock distribuído/externo (Redis, filesystem).

---

# 6. Lock-wait / lock-timeout semantics

O bootstrap usa `pg_try_advisory_lock()` em loop de retry limitado contra um deadline monotônico — nunca um `pg_advisory_lock()` bloqueante e ilimitado como comportamento externamente observável.

Após acquisition (por qualquer participante, incluindo o segundo de dois bootstraps concorrentes), o estado é **sempre re-lido do zero a partir do PostgreSQL** — nunca de um snapshot em memória capturado antes da espera começar.

```text
bootstrap A: acquire lock -> classify fresh -> migrate se necessário
             -> verify exact head -> release lock
bootstrap B: (bloqueado esperando a mesma key enquanto A segura)
             -> acquire lock (agora que A liberou)
             -> RE-READ estado do banco do zero
             -> observa que já está em exact head
             -> no-op
             -> release lock
```

**Nenhuma execução de migration de dois bootstraps automáticos ADR-0015 pode rodar concorrentemente contra o mesmo banco.** Essa garantia é escopada precisamente ao caminho cooperativo deste feature — não cobre uma invocação manual/administrativa concorrente da CLI Alembic fora desse mecanismo (ver §13.1).

Quando o deadline expira sem adquirir o lock:

```text
log "migration_lock_timeout"
libera a conexão dedicada (nunca mantida aberta indefinidamente)
permanece em BOOTSTRAP_MAINTENANCE
o loop externo de retry já existente do _run_bootstrap tenta novamente depois
```

Isto é classificado como **pre-migration transient failure** (§10.1) — seguro para retry indefinido pelo mecanismo já existente. Não existe timeout de lock-wait configurável publicamente sem uma necessidade concretamente demonstrada — o valor do deadline é uma constante de implementação interna.

---

# 7. Subprocess execution — invocação do Alembic

O bootstrap invoca `alembic upgrade head` como um **processo OS real** (ex.: `asyncio.create_subprocess_exec`), aguardado non-blockingly a partir da coroutine de bootstrap já existente. **Nunca** chama `alembic.command.upgrade(...)` in-process.

Isto é um requisito técnico concreto, não preferência de estilo: `migrations/env.py`'s `run_migrations_online()` chama `asyncio.run(...)`, que levanta `RuntimeError: asyncio.run() cannot be called from a running event loop` se invocado in-process a partir de uma coroutine já rodando sob o event loop do `uvicorn`. Subprocess invocation evita isso inteiramente e requer **zero mudanças em `migrations/env.py`**.

## 7.1 Sem privilege separation

O subprocess roda no **mesmo** contexto OS/container, com o **mesmo** `DATABASE_URL` e o **mesmo** role PostgreSQL do processo de servidor de longa duração. Este feature **não é**, e não afirma ser, um mecanismo de database-privilege-separation. Sua vantagem real é isolamento de processo/event-loop: a execução de DDL acontece em um processo distinto com seu próprio event loop, reusando o caminho de execução CLI já existente e inalterado do Alembic, em vez de mutar o estado do interpretador do servidor de longa duração in place.

---

# 8. Migration execution — sem timeout genérico

Uma vez que o subprocess de migration tenha efetivamente começado, **nenhum timeout automático de wall-clock genérico** o mata. Matar DDL no meio da execução por causa de um timer arbitrário é menos seguro que deixá-lo terminar — uma statement DDL interrompida pode deixar objetos em estado inconsistente mais difícil de raciocinar que "ainda rodando". `/health/live` permanece alcançável e `/health/ready` permanece `NOT_READY` durante toda a duração (D33, inalterado) — uma migration longa é observável como "ainda em bootstrap", nunca indistinguível de um processo travado.

---

# 9. Post-migration verification

`alembic upgrade head` aplicando múltiplas migrations pendentes **não é assumido atômico** — Alembic comita por-migration por default, não o batch inteiro como uma única transação (a exceção deliberada já existente neste projeto, migration `0011`'s `autocommit_block()`, é não-transacional por exigência do próprio PostgreSQL, não por batching do Alembic). Portanto o bootstrap sempre:

```text
lê estado autoritativo do banco
-> tenta migration
-> lê estado autoritativo do banco de novo
```

e nunca persiste um booleano local como `migration_done = true` em substituição a re-ler o revision state real do banco. Exit code zero do subprocess **não é suficiente** — o re-read via readiness check (`readiness.py`, reutilizado) deve confirmar exact head antes de o bootstrap prosseguir.

---

# 10. Failure semantics — três classes explicitamente distintas

## 10.1 Pre-migration transient failure

Lock-wait timeout, falha de conectividade PostgreSQL, Neo4j inalcançável, ou qualquer falha *antes* de um subprocess de migration ser efetivamente spawned. Comportamento: inalterado — logado, o attempt inteiro de bootstrap é retentado pelo loop externo já existente no intervalo fixo já existente. Seguro para retry incondicional — nada foi tentado contra o schema ainda.

## 10.2 Migration execution failure — sticky por processo

Um subprocess de migration foi efetivamente spawned e saiu non-zero, **ou** a post-migration verification (§9) encontra que o banco **não** está no exact head esperado após o subprocess sair zero.

```text
migration_failed_this_process = true   (em memória, estado deste bootstrap —
                                         nunca uma nova tabela/coluna)
```

Pelo resto do lifetime daquele processo: **Alembic nunca é invocado automaticamente de novo.** O processo permanece em `BOOTSTRAP_MAINTENANCE`, `/health/live` permanece alcançável, `/health/ready` permanece `NOT_READY`, e o probe read-only já existente **continua rodando** no intervalo de retry já existente — exclusivamente para observar se um operador corrigiu o problema manualmente. Se esse probe observar depois que o banco alcançou exact head, o bootstrap prossegue para `OPERATIONAL` **sem exigir restart do processo**.

Um estado "ancestral conhecido" (§4) e "uma execução de migration já falhou neste processo" são dois fatos independentes e simultaneamente verdadeiros — apenas o segundo bloqueia futura invocação automática do Alembic dentro daquele lifetime de processo. Migration automática **nunca** é retentada incondicionalmente a cada 5 segundos uma vez genuinamente tentada e falhada.

**Nenhuma tabela ou coluna `migration_failure` durável é introduzida** para fazer essa stickiness sobreviver a um restart de processo. Após um restart, o bootstrap classifica estado autoritativo do zero — se o banco ainda for um estado ancestral válido, um **novo lifetime de processo** pode fazer uma nova tentativa automática. Isto é uma consequência deliberada e explicitamente aceita: o requisito real é "nenhum retry DDL ilimitado de 5 segundos dentro de um processo", não "eliminar toda tentativa repetida possível através de todo restart possível".

## 10.3 Schema-invalid / classification failure

Múltiplos heads, revision divergente, banco à frente da aplicação, ou qualquer outro estado que a tabela de classificação (§4) marca "fail closed". Nenhuma migration é tentada para esses estados. Comportamento inalterado do `revision_mismatch`-shaped fail-closed retry já existente hoje.

---

# 11. Graceful shutdown — migration critical section

**Uma vez que o subprocess Alembic foi spawned, o subprocess de migration e a ownership do advisory lock formam uma única migration critical section.** Enquanto essa critical section estiver aberta, um **graceful application shutdown NÃO PODE**:

```text
abandonar o child process
liberar o advisory lock enquanto o child ainda está vivo
completar graceful shutdown enquanto o child de migration ainda está vivo
```

Se shutdown/cancellation é requisitado enquanto uma migration está executando, a sequência obrigatória é:

```text
shutdown fica pendente (não imediato)
a supervisão da migration continua
o bootstrap mantém a conexão do advisory lock viva
a aplicação espera o child Alembic terminar naturalmente
o resultado do child (exit code) é observado e classificado normalmente
  (sucesso -> verify; falha -> sticky migration-execution-failure, §10.2,
  inalterado pelo shutdown estar em progresso)
só então o advisory lock é liberado
só então o graceful shutdown pode completar
```

Este contrato não congela qual primitiva Python específica implementa isso (cancellation shielding, uma supervising task aguardada fora do escopo cancelado, ou mecanismo equivalente) — escolha de implementação deliberada, não ambiguidade por omissão. Nenhum timeout genérico é introduzido para limitar quanto tempo o graceful shutdown pode esperar.

---

# 12. Hard termination — caso distinto, não bug do shutdown path

`SIGKILL`, hard kill de container, ou crash de processo **não são** graceful shutdown, e nenhuma semântica graceful é tentada para eles:

```text
processo/conexões terminam imediatamente, incluindo o child e a conexão
  do advisory lock
PostgreSQL eventualmente libera o advisory lock session-level sozinho
  (propriedade de crash-safety do próprio lock)
a próxima tentativa de processo de aplicação re-classifica o estado
  autoritativo do banco do zero, exatamente como todo outro restart já faz
```

**Nenhuma garantia de completion é feita ou implicada** para uma migration interrompida por hard termination — apenas que o lock não vaza, e que a próxima tentativa parte de uma leitura autoritativa fresca do banco, nunca de uma suposição sobre o que o attempt morto pode ter terminado.

---

# 13. Manual Alembic CLI — permanece totalmente suportado

`alembic upgrade head`, `alembic current` e `alembic heads` permanecem disponíveis como ferramentas administrativas dentro da release image, inalteradas. O bootstrap automático é uma conveniência operacional e guard rail sobre o Alembic, nunca um substituto, e nunca o esconde. Nenhum endpoint HTTP de migration (`POST /migrate` ou equivalente) é introduzido — migration permanece alcançável apenas pelo caminho CLI/subprocess já existente, nunca pela API pública.

## 13.1 Regra operacional de concorrência

**Uma mutação manual/raw do Alembic (`upgrade`, `downgrade`, `stamp`, etc.) NÃO DEVE rodar concorrentemente com um bootstrap automático cujo subprocess Alembic esteja em voo** — o advisory lock protege participantes cooperativos do bootstrap entre si, não o banco contra um operador correndo manualmente por fora desse mecanismo. Migration manual permanece seguramente utilizável sempre que, por exemplo: o processo de aplicação está parado; `DATABASE_MIGRATION_MODE=verify_only` está em efeito (o bootstrap nunca invoca Alembic nesse modo); ou o processo em execução já está no estado sticky migration-failed/read-only (§10.2) e portanto só realiza probes read-only, nunca spawna um novo subprocess Alembic por conta própria. Esta é uma disciplina operacional/documental — o mesmo padrão já implícito hoje para dois operadores que poderiam rodar `alembic upgrade head` manualmente ao mesmo tempo.

---

# 14. Process ordering (sequência ao redor, inalterada)

```text
processo FastAPI inicia (uvicorn, CMD inalterado)
  -> BOOTSTRAP_MAINTENANCE (inalterado)
  -> superfície HTTP de manutenção alcançável (inalterado, D33)
  -> migration bootstrap (NOVO, substitui o check read-only-only de hoje):
       connect (conexão dedicada)
       -> acquire advisory lock (bounded try+retry, §6)
       -> re-read estado autoritativo do banco
       -> classifica contra o Alembic DAG (§4)
       -> exact head: no-op
       -> estado `auto` permitido: spawna subprocess `alembic upgrade head`,
          aguarda completion
       -> re-executa o readiness check read-only já existente para VERIFICAR
          exact head foi realmente alcançado (§9)
       -> libera advisory lock
  -> Neo4j bootstrap (inalterado)
  -> pipeline recovery (inalterado)
  -> worker start, restrições de claim já existentes (inalterado)
  -> storage convergence quando STORAGE_BACKEND=s3 (inalterado, ADR-0011)
  -> OPERATIONAL (inalterado)
```

Nenhum novo process state é introduzido. `BOOTSTRAP_MAINTENANCE`, o retry loop fail-closed já existente e o contrato de liveness D33 são reutilizados exatamente como já existem.

---

# 15. Restore contract

Porque `auto` migra **qualquer** estado ancestral válido automaticamente, um operador que restaura ou quer inspecionar um backup histórico na sua revision **original** (em vez de transformá-lo imediatamente para frente) deve iniciar a aplicação com `DATABASE_MIGRATION_MODE=verify_only` para essa sessão. Isto é o mecanismo explícito e documentado que mantém "historical restore" e "implicit forced upgrade" como escolhas distintas do operador, em vez de se tornarem a mesma ação por default.

Consequência explicitamente documentada: com `auto` como default, iniciar uma imagem current-head contra uma revision histórica válida ancestral do head irá **automaticamente migrá-la para frente no primeiro boot** — uma mudança material de comportamento relativa ao contrato pré-v0.6.0, onde o operador decidia explicitamente se e quando avançar um banco restaurado.

---

# 16. Rollback contract — sempre fail-closed, nunca automático

Um banco com revision mais novo que o head da aplicação corrente (cenário de application rollback: operador inicia uma imagem mais antiga contra um banco já migrado para frente) é **sempre fail closed**, em ambos os modos (§4, última linha). O bootstrap **nunca** tenta `alembic downgrade`, `stamp`, ou qualquer outro "repair" automático nesse cenário. Application rollback e schema rollback permanecem duas operações distintas; apenas a primeira é implicada por iniciar uma imagem mais antiga.

---

# 17. Migration authoring rule para candidatos automáticos

Este feature **não** congela "toda migration deve ser additive" — mais estrito que o próprio histórico de migrations do projeto (`0010` ativa uma constraint previamente deferida; `0011` adiciona um enum value nativo sem downgrade seguro; nenhuma das duas é puramente additive, e ambas já foram lançadas com segurança). Em vez disso:

> Toda migration automática deve ser seguramente resumível a partir de qualquer revision Alembic committed alcançada antes de uma interrupção.

Concretamente: um passo de migration transacional deve depender da DDL transacional do próprio PostgreSQL onde aplicável (o default de quase toda migration no histórico deste projeto); qualquer passo explícito não-transacional/autocommit deve ser idempotente ou de outra forma seguramente re-executável. `0011` é o precedente já existente do projeto para o segundo caso (`ALTER TYPE ... ADD VALUE IF NOT EXISTS`, dentro de `autocommit_block()`).

---

# 18. Zero new public API

```text
novo endpoint HTTP público = 0    (sem POST /migrate, sem migration admin API)
```

Migration permanece alcançável exclusivamente através do caminho CLI/subprocess já existente.

---

# 19. Zero new schema para este próprio feature

```text
nova application table para este feature = 0   (advisory lock não precisa de tabela)
nova Alembic revision para este feature = 0     (este feature muda comportamento
                                                   de bootstrap, não schema)
```

Migrations `0018`+ eventualmente necessárias para outro propósito de produto permanecem fora do escopo deste feature especificamente — este feature em si não requer nenhuma.

---

# 20. Zero Neo4j / graph_outbox changes

Este feature não toca Neo4j, `graph_outbox`, projeção, ou qualquer worker de grafo. O bootstrap de migration acontece estritamente antes de Neo4j bootstrap na ordenação existente (§14) e não introduz nenhuma nova interação com essa camada.

---

# 21. Deployment portability boundary

A correção desta arquitetura nunca pode depender de:

```text
Docker Compose `service_completed_successfully` dependency condition
suporte a init-container de qualquer tipo
hooks específicos do EasyPanel
hooks específicos do Portainer
semântica de lifecycle do Docker Swarm / `docker stack deploy`
```

Porque a arquitetura escolhida (lifespan-based, dentro do único processo de aplicação já existente) não precisa de nenhum dos itens acima, ela funciona identicamente sob Compose puro, Portainer (que consome `compose.yaml` diretamente) e EasyPanel, com a mesma imagem e o mesmo código em todos os casos. Qualquer um desses mecanismos de orquestração pode existir depois como conveniência complementar — nunca como o que a garantia de segurança deste feature depende.

---

# 22. Sem promessa de rolling-upgrade compatibility

O modelo de deployment real e documentado deste projeto é stop-old-then-start-new (Docker Swarm confirmado não usado em nenhum lugar), não um rolling update com versões antiga e nova rodando concorrentemente contra um schema em transição. Este feature não exige, e não tenta garantir, que uma versão antiga e uma nova da aplicação possam operar concorrentemente com segurança contra um schema recém-migrado. A regra "seguramente resumível a partir de qualquer revision committed" (§17) é necessária mas não suficiente para essa propriedade mais forte.

---

# 23. Documentation supersession list (execução futura em SM-904)

Os seguintes documentos afirmam hoje, em substância, "migration é explícita, nunca automática", e precisarão de atualização em SM-904 (não neste contrato):

```text
README.md
docs/operations.md
docs/deployment/easypanel.md
AGENTS.md
CLAUDE.md
```

`docs/adr/0011-...md` recebe apenas uma **nota de forward-reference estreita** apontando para ADR-0015 — nunca é reescrito; permanece o registro histórico do contrato como originalmente congelado.

---

# 24. Deployment test replacement requirement

`tests/unit/test_deployment_compose.py::test_compose_files_never_auto_run_alembic` deve ser **substituído, nunca deletado**, por um invariante de intenção equivalente: Compose nunca deve invocar migration Alembic raw/não-serializada diretamente como `command:`/`entrypoint:`; migration automática é permitida apenas através do caminho de bootstrap sancionado, locked e verificado deste feature. Esta substituição é trabalho de SM-904 (deployment/static contract integration, junto da documentação operacional) — SM-905 apenas executa/verifica esse gate como parte da suite completa, não o define pela primeira vez. Não é trabalho deste contrato.

---

# 25. Observability

Os seguintes eventos de log estruturado estão congelados conceitualmente (nomes de campo/schema exatos são detalhe de implementação):

```text
migration_bootstrap_started
migration_lock_waiting
migration_lock_acquired
migration_lock_timeout
migration_not_required
migration_upgrade_started
migration_upgrade_succeeded
migration_upgrade_failed
migration_schema_invalid
migration_bootstrap_verified
```

Nenhum desses eventos, nem qualquer outro logging introduzido por este bootstrap, pode incluir `DATABASE_URL`, senha, ou qualquer outro material de credencial.

---

# 26. Security / privileges

O bootstrap usa o **mesmo** `DATABASE_URL`/role já usado pela aplicação em runtime — nenhum role de banco dedicado a migration é introduzido por este feature. Não é uma exposição nova: um operador rodando `alembic upgrade head` manualmente hoje já usa exatamente essa mesma connection string e role. Uma credencial dedicada a migration, separada da credencial de runtime, é registrada como **possibilidade futura de hardening**, não escopo atual.

---

# 27. Out of scope — v0.6.0

Permanentemente fora de escopo desta release (não "ainda não implementado"):

```text
novos endpoints HTTP públicos além dos já existentes
migrations de schema não relacionadas a este feature
migration audit table durável
dedicated migration database role
Redis / external lock service
rolling deployments / rolling-upgrade compatibility formal
downgrade/stamp/repair automático de schema
Compose one-shot/init-container como correctness boundary
timeout genérico de execução de migration
multi-tenant migration coordination
AgentRevision, AgentRun, semantic Agent resolve, ou qualquer item
  já congelado como fora de escopo por releases anteriores
```

---

# 28. Invariants obrigatórios

```text
PostgreSQL permanece fonte única de verdade do schema
Alembic permanece única autoridade de evolução de schema
Alembic é sempre invocado via subprocess OS -- nunca via API Python in-process
pristine fresh schema e unversioned non-empty schema nunca são colapsados
  em uma única categoria auto-migrável
unversioned non-empty schema é sempre fail-closed, em ambos os modos
ancestralidade é sempre determinada pela API oficial de revision-graph
  do Alembic -- nunca por parsing manual
nenhuma migration executa antes da classificação estar completa
no máximo uma execução de migration automática roda por vez contra o
  mesmo banco (advisory lock)
falha de execução de migration é sticky por lifetime de processo --
  nunca retentada automaticamente a cada 5 segundos
graceful shutdown nunca libera o advisory lock enquanto o child Alembic
  está vivo
hard termination nunca garante completion de migration -- o lock nunca vaza
rollback de schema nunca é automático
a CLI Alembic manual permanece totalmente suportada e nunca é substituída
`/health/live` permanece alcançável durante toda a duração do bootstrap
nenhum endpoint HTTP de migration é introduzido
nenhuma tabela/coluna nova é introduzida por este feature em si
Neo4j/graph_outbox permanecem intocados por este feature
a correção nunca depende de mecanismo específico de orquestrador
```

---

# 29. Critério de conclusão do v0.6.0

O v0.6.0 estará pronto para release apenas quando (ver backlog técnico, GATE-v0.6.0):

- `DATABASE_MIGRATION_MODE=auto|verify_only` estiver implementado, com `auto` como default, exatamente como especificado neste contrato;
- toda linha da tabela de classificação (§4) estiver comprovada por teste, incluindo a distinção pristine vs. unversioned-non-empty;
- o advisory lock (§5–§6) estiver comprovado com PostgreSQL real, incluindo exatamente uma execução de migration entre dois bootstraps concorrentes;
- a sticky failure semantics (§10.2) estiver comprovada — uma falha de migration não é retentada no intervalo de 5 segundos já existente;
- a migration critical section de graceful shutdown (§11) estiver comprovada;
- `tests/unit/test_deployment_compose.py` tiver sido substituído por um invariante equivalente (§24);
- toda a documentação listada em §23 tiver sido atualizada;
- nenhum endpoint público novo, nenhuma tabela nova para este feature, e nenhuma mudança em Neo4j/`graph_outbox` existirem;
- o Feature Contract for promovido de `Proposed` para `Implemented` apenas após os gates acima passarem — nunca antes.
