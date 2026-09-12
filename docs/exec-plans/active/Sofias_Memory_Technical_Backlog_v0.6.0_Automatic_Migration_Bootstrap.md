# Sofias Memory — Backlog Técnico Executável v0.6.0 Automatic Serialized Migration Bootstrap

**Release:** v0.6.0\
**Feature:** Automatic Serialized Migration Bootstrap\
**Status:** TODO\
**Sequência:** SM-901..SM-906\
**Regra de execução:** executar uma task por vez; não antecipar dependências ou escopo de tickets posteriores.

## 1. Objetivo

O v0.6.0 elimina o passo manual `alembic upgrade head` do caminho comum de upgrade, introduzindo um automatic, serialized, fail-closed migration bootstrap integrado à sequência de startup já existente (`lifespan.py`), conforme ADR-0015 (accepted) e o Feature Contract v0.6.0.

Ao final deste backlog, Sofias Memory deverá possuir:

- `DATABASE_MIGRATION_MODE=auto|verify_only` (default `auto`) em `sofias_memory/config.py`;
- classificação de schema state pela API oficial de revision-graph do Alembic, com pristine fresh schema e unversioned non-empty schema como estados distintos, o segundo sempre fail-closed;
- advisory lock session-level dedicado, com acquisition bounded try+retry e re-read de estado autoritativo após acquisition;
- invocação do Alembic exclusivamente via subprocess OS, nunca via API Python in-process;
- sticky migration-execution-failure semantics por lifetime de processo, sem retry automático no intervalo de 5 segundos existente;
- migration critical section respeitada por graceful shutdown (lock nunca liberado enquanto o child Alembic está vivo);
- `tests/unit/test_deployment_compose.py::test_compose_files_never_auto_run_alembic` substituído por um invariante equivalente;
- documentação operacional (README, operations, easypanel, AGENTS.md/CLAUDE.md) atualizada para refletir o novo contrato;
- hardening real contra PostgreSQL e smoke de release image;
- version bump `0.5.0 -> 0.6.0` apenas na task final de release.

Este backlog não implementa migration audit table, dedicated migration database role, rolling-upgrade compatibility, ou qualquer repair/inference automático de schema não versionado — non-goals permanentes conforme Feature Contract §27.

---

# 2. Fontes normativas

Ordem de precedência específica deste release:

1. instrução explícita da task em execução;
2. `AGENTS.md`;
3. ADR-0015 — Automatic Serialized Migration Bootstrap (accepted);
4. Feature Contract v0.6.0 — Automatic Serialized Migration Bootstrap;
5. ADRs anteriores aplicáveis, especialmente ADR-0011 (D31/D32/D33, exceto a clausula "never automatic" explicitamente superseded por ADR-0015) e ADR-0009 (retry discipline reutilizada);
6. contratos e testes existentes, especialmente `tests/unit/test_deployment_compose.py`;
7. PRD original, onde não amended pelo ADR-0015.

O Feature Contract define a semântica pública detalhada. ADR-0015 define a mudança arquitetural e suas fronteiras. Este backlog define somente a ordem executável de implementação e os gates.

---

# 3. Invariantes de release

Durante SM-901..SM-906:

- PostgreSQL permanece fonte única de verdade do schema;
- Alembic permanece única autoridade de evolução de schema;
- Alembic é sempre invocado via subprocess OS — nunca via `alembic.command.upgrade(...)` in-process;
- pristine fresh schema e unversioned non-empty schema nunca são colapsados em uma única categoria auto-migrável;
- unversioned non-empty schema é sempre fail-closed, em ambos os modos;
- ancestralidade é sempre determinada pela API oficial de revision-graph do Alembic (`ScriptDirectory`), nunca por parsing manual de nomes/strings;
- no máximo uma execução de migration automática roda por vez contra o mesmo banco;
- falha de execução de migration é sticky por lifetime de processo, nunca retentada automaticamente no intervalo de 5 segundos;
- graceful shutdown nunca libera o advisory lock enquanto o child Alembic está vivo;
- hard termination nunca garante completion de migration, mas nunca deixa o lock vazar;
- `/health/live` permanece alcançável durante toda a duração do bootstrap (D33 preservado);
- nenhum endpoint HTTP de migration é introduzido;
- nenhuma tabela/coluna nova é introduzida por este feature em si;
- Neo4j/`graph_outbox` permanecem intocados;
- não introduzir migration audit table, dedicated migration role, Redis, external lock service, ou rolling-upgrade support.

---

# 4. Sequência

| Ticket | Entrega principal | Depende de | Migration | Status |
|---|---|---|---|---|
| SM-901 | Schema classification model (sem executar Alembic) | — | — | TODO |
| SM-902 | Advisory lock + subprocess bootstrap | SM-901 | — | TODO |
| SM-903 | Sticky failure + graceful shutdown critical section + hard-termination orphan safety | SM-902 | — | TODO |
| SM-904 | Operational/deployment documentation integration + Compose deployment/static invariant | SM-903 | — | TODO |
| SM-905 | Real-PostgreSQL hardening matrix + release-image smoke + orphan-safety scenario | SM-903, SM-904 | — | TODO |
| SM-906 | Release prep — quality gates, version bump, GATE-v0.6.0 | SM-905 | — | TODO |

---

# SM-901 — Schema classification model

## Objetivo

Construir o modelo puro de classificação de schema state (§4 do Feature Contract), sem ainda executar Alembic e sem ainda tocar `lifespan.py`. Esta task é fundação de domínio, não integração de bootstrap.

## Escopo

**Nota (erratum pré-implementação):** o Feature Contract §4 foi corrigido antes desta task começar — o antigo estado "revision do banco mais novo que o head" nunca foi diretamente detectável pelo `ScriptDirectory` (uma revision ausente do grafo da imagem corrente levanta erro de resolução em `get_revision()`/`get_revisions()`/`iterate_revisions()`, seja ela uma revision futura genuína ou uma revision estrangeira não relacionada — indistinguíveis sem comparação lexical/numérica de IDs, que é proibida). Os 9 estados abaixo são os estados **observáveis pela imagem atual**, já corrigidos; esta task implementa exatamente eles, não a versão anterior da tabela.

Implementar:

- função/módulo que classifica o estado do banco em exatamente os 9 estados observáveis do Feature Contract §4 — `PRISTINE_FRESH_SCHEMA`, `UNVERSIONED_NON_EMPTY_SCHEMA`, `EXACT_HEAD`, `KNOWN_ANCESTOR`, `CODE_MULTIPLE_HEADS`, `VERSION_TABLE_EMPTY`, `DATABASE_MULTIPLE_REVISIONS`, `KNOWN_NON_ANCESTOR`, `UNRECOGNIZED_REVISION` — usando exclusivamente a API oficial de revision-graph do Alembic (`alembic.script.ScriptDirectory.get_heads()`, `get_revision()`/`get_revisions()`, `iterate_revisions()` ou equivalente da versão instalada) — nunca parsing manual de nomes de arquivo ou `down_revision`, nunca comparação lexical/numérica de revision IDs;
- distinção explícita e nunca colapsada entre `PRISTINE_FRESH_SCHEMA` (`alembic_version` ausente **e** nenhuma application base table) e `UNVERSIONED_NON_EMPTY_SCHEMA` (`alembic_version` ausente **e** uma ou mais application base tables existem);
- snapshot de classificação que retém explicitamente `alembic_version_table_present: bool` (ou representação imutável equivalente), nunca apenas um `frozenset[str]` de revisions que colapsaria "tabela ausente" e "tabela presente com zero linhas" no mesmo valor vazio — essa distinção é o que separa `VERSION_TABLE_EMPTY` (§4.6) de `PRISTINE_FRESH_SCHEMA`/`UNVERSIONED_NON_EMPTY_SCHEMA` (Feature Contract §4.11); a forma exata (dataclass/campos) é escolha desta task, a informação retida não é;
- detecção de `CODE_MULTIPLE_HEADS` (múltiplos heads no código Alembic da aplicação, code-side);
- detecção de `DATABASE_MULTIPLE_REVISIONS` (múltiplas linhas de revision registradas no banco, database-side);
- detecção de `VERSION_TABLE_EMPTY` (`alembic_version` existe como base table, mas zero linhas de revision) como estado distinto de `PRISTINE_FRESH_SCHEMA` — nunca tratado como fresh install;
- detecção de `KNOWN_NON_ANCESTOR` (revision resolvível pelo `ScriptDirectory`, mas não provável como ancestral do head) e de `UNRECOGNIZED_REVISION` (revision não resolvível pelo `ScriptDirectory` — categoria que subsume tanto uma revision estrangeira/não relacionada quanto um rollback genuíno de imagem, sem tentar distingui-los);
- leitura da configuração `database_migration_mode: Literal["auto", "verify_only"]` em `sofias_memory/config.py`, com `alias="DATABASE_MIGRATION_MODE"` e `default="auto"`, seguindo o template já existente de `storage_backend`.

## Não fazer

- não spawnar nenhum subprocess Alembic nesta task;
- não tocar `lifespan.py`/`_attempt_bootstrap` nesta task;
- não implementar advisory lock nesta task (SM-902);
- não implementar graceful shutdown handling nesta task (SM-903);
- não atualizar nenhuma documentação operacional nesta task (SM-904);
- não tentar distinguir, dentro de `UNRECOGNIZED_REVISION`, se a revision é futura/mais nova, estrangeira, ou danificada — nenhuma heurística de string, nenhuma comparação lexical/numérica (`"0018" > "0017"` e equivalentes são explicitamente proibidos).

## Gate SM-901

A task só encerra quando houver teste unitário dedicado provando cada um dos nove estados observáveis, especificamente:

```text
alembic_version ausente + zero base tables       -> PRISTINE_FRESH_SCHEMA
alembic_version ausente + >=1 base table          -> UNVERSIONED_NON_EMPTY_SCHEMA
alembic_version presente + zero linhas de revision -> VERSION_TABLE_EMPTY
alembic_version presente + >1 linha de revision    -> DATABASE_MULTIPLE_REVISIONS
uma revision, ancestral conhecido do head          -> KNOWN_ANCESTOR
uma revision, igual ao único head conhecido        -> EXACT_HEAD
uma revision, resolvível mas não-ancestral do head -> KNOWN_NON_ANCESTOR
uma revision, não resolvível pelo ScriptDirectory  -> UNRECOGNIZED_REVISION
múltiplos heads no código da aplicação             -> CODE_MULTIPLE_HEADS
```

cobrindo `auto` e `verify_only` onde o comportamento difere; e além disso:

- `PRISTINE_FRESH_SCHEMA` e `UNVERSIONED_NON_EMPTY_SCHEMA` são comprovadamente tratados como estados distintos por teste (nunca o mesmo branch de código);
- `VERSION_TABLE_EMPTY` é comprovadamente distinto de `PRISTINE_FRESH_SCHEMA`/`UNVERSIONED_NON_EMPTY_SCHEMA` por teste — o snapshot de classificação nunca colapsa "tabela ausente" e "tabela presente com zero linhas" no mesmo valor;
- um teste prova explicitamente que `UNRECOGNIZED_REVISION` **não** tenta inferir futuro/anterior/estrangeiro a partir da string da revision (nenhuma comparação lexical/numérica em nenhum ponto do código de classificação, comprovado por revisão do diff além do teste);
- a detecção de ancestralidade (`KNOWN_ANCESTOR` vs. `KNOWN_NON_ANCESTOR`) é comprovada usando a API oficial do Alembic, nunca uma implementação paralela de parsing;
- `database_migration_mode` está presente em `Settings` com o `Literal[...]`/alias/default congelados acima, comprovado por teste de config;
- nenhum subprocess Alembic é invocado em nenhum teste desta task;
- suite existente permanece verde.

---

# SM-902 — Advisory lock + subprocess bootstrap

## Objetivo

Integrar a classificação do SM-901 ao bootstrap real: advisory lock, subprocess Alembic, e a substituição do check read-only-only de hoje pelo fluxo completo definido no Feature Contract §5–§9 e §14, dentro de `_attempt_bootstrap`.

## Escopo

Implementar:

- advisory lock session-level (`pg_try_advisory_lock`/`pg_advisory_unlock`) em conexão dedicada, key fixa e documentada (bigint 63-bit-safe), nunca transaction-level;
- loop de acquisition bounded try+retry contra deadline monotônico interno (não configurável publicamente sem necessidade demonstrada); em timeout, classifica como pre-migration transient failure (§10.1 do Feature Contract) e libera a conexão;
- re-read do estado autoritativo do banco **após** adquirir o lock, sempre, nunca reusando classificação anterior à espera;
- spawn do subprocess `alembic upgrade head` via `asyncio.create_subprocess_exec` (ou equivalente), aguardado non-blockingly, nunca via `alembic.command.upgrade(...)` in-process;
- post-migration verification: re-execução do readiness check read-only já existente para confirmar exact head — exit code zero do subprocess isoladamente nunca é suficiente;
- integração na ordenação de `_attempt_bootstrap`, na posição exata hoje ocupada pelo check read-only, estritamente antes de Neo4j bootstrap/`worker.start()`/storage convergence.

## Não fazer

- não implementar sticky failure semantics nesta task além do necessário para não repetir imediatamente — a stickiness formal e sua prova são SM-903;
- não implementar graceful shutdown/critical section handling nesta task — SM-903;
- não modificar `migrations/env.py`;
- não introduzir timeout de execução de migration.

## Gate SM-902

A task só encerra quando:

- dois bootstraps concorrentes contra o mesmo banco real (PostgreSQL, não mock) resultam em **exatamente uma** execução de migration; o segundo, após adquirir o lock, re-lê o estado e observa já-migrado, sem spawnar Alembic de novo — comprovado por teste de integração real;
- lock-wait timeout é comprovado como não-fatal e retentado pelo loop externo já existente, comprovado por teste;
- unlock bem-sucedido e liberação do lock em perda de conexão/crash são comprovados por teste real PostgreSQL;
- um fresh, genuinamente pristine database migra automaticamente para head sob `auto`, comprovado por teste de integração;
- um unversioned database com application tables falha closed com **zero** invocação de Alembic (provado diretamente, não apenas inferido do estado final), comprovado por teste de integração;
- ancestral conhecido migra para head; exact head é no-op; ambos comprovados por teste;
- post-migration verification é comprovada rejeitando um subprocess exit-zero cujo estado final não é exact head (cenário simulado);
- `migrations/env.py` permanece byte-a-byte inalterado;
- suite existente permanece verde.

---

# SM-903 — Sticky failure + graceful shutdown critical section + hard-termination orphan safety

## Objetivo

Provar formalmente as três semânticas mais sensíveis do ADR-0015: sticky migration-execution-failure (nunca um retry loop de DDL a cada 5 segundos), a migration critical section durante graceful shutdown (o advisory lock nunca é liberado com o child Alembic ainda vivo), e o invariante de orphan-safety sob hard termination (Feature Contract §12, erratum) — uma migration Alembic nunca permanece capaz de mutar o banco depois que sua proteção de serialização foi liberada, mesmo quando o supervisor da aplicação morre sem aviso.

## Escopo

### Sticky failure (Feature Contract §10.2)

- `migration_failed_this_process` como estado em memória do processo de bootstrap (nunca tabela/coluna nova);
- prova explícita de que, após uma falha genuína de execução de migration, tentativas subsequentes do loop de retry externo já existente **não** re-invocam Alembic — apenas o probe read-only continua rodando;
- prova de que, se o probe read-only subsequentemente observa exact head (operador corrigiu manualmente, out-of-band), o processo prossegue para `OPERATIONAL` **sem restart**;
- prova de que um restart de processo re-classifica do zero e, se o banco ainda for um estado ancestral válido, um novo lifetime de processo pode fazer nova tentativa automática.

### Graceful shutdown — migration critical section (Feature Contract §11)

- implementação da migration critical section: uma vez spawned, o subprocess e a ownership do advisory lock formam uma unidade; shutdown/cancellation fica pendente, a supervisão continua, a conexão do lock permanece viva, a aplicação espera o child terminar naturalmente, o resultado é classificado normalmente, só então o lock é liberado e só então o shutdown completa;
- prova de que uma segunda tentativa de bootstrap iniciando durante essa janela de espera não consegue começar uma nova migration (bloqueada no lock).

### Hard termination — orphan-safety invariant (Feature Contract §12, erratum)

Este subitem implementa o invariant normativo do erratum de hard-termination serialization: uma execução de migration Alembic nunca permanece capaz de mutar o banco depois que sua proteção de serialização foi liberada — o lifetime do child de migration nunca sobrevive à proteção que o serializa, sob graceful shutdown, crash do processo pai, `SIGKILL` do supervisor, ou qualquer outra terminação inesperada do supervisor.

- escolher e implementar um mecanismo concreto que satisfaça pelo menos uma das duas propriedades equivalentes congeladas pelo erratum: (A) a árvore de processo/child de migration não pode sobreviver ao supervisor dono do lock; ou (B) se o child pode sobreviver ao supervisor, a ownership de serialização em si permanece viva e mantida até o child terminar. Esta task escolhe o mecanismo (ex.: process-group supervision, parent-death signalling, container/process-tree lifecycle enforcement, guardian dedicado, ou outro equivalente) avaliando runtime Docker de release, execução local/source, comportamento de subprocess do `asyncio`, fronteira de suporte cross-platform e testabilidade — o Feature Contract não escolhe por ela;
- separação explícita de hard termination (SIGKILL/crash) do caminho graceful: nenhuma tentativa de dar semântica graceful a esse caso;
- prova de que PostgreSQL libera o advisory lock por conta própria quando sua sessão dona termina, mas que essa liberação de lock, isoladamente, **não** é tratada como prova de que o child de migration também terminou;
- prova de que, quando o processo seguinte adquire legitimamente o advisory lock (porque o lock foi liberado e a migration anterior está de fato terminada ou permanece protegida), ele sempre re-lê o estado autoritativo do PostgreSQL do zero, nunca assumindo completion do attempt anterior.

## Não fazer

- não introduzir tabela/coluna `migration_failure` durável;
- não introduzir timeout genérico de graceful shutdown;
- não alterar a classificação do SM-901 nem o fluxo de acquisition do SM-902 além do necessário para observar/reagir ao resultado do child;
- não escolher previamente entre as propriedades A/B do orphan-safety invariant fora desta task — essa escolha é desta task, não de uma decisão anterior do backlog.

## Gate SM-903

A task só encerra quando:

- uma falha genuína de migration é comprovada **não** retentada no intervalo de 5 segundos existente, por teste real PostgreSQL que observa múltiplos ciclos do loop de retry sem nova invocação de Alembic;
- a recuperação sem restart via probe read-only após fix manual out-of-band é comprovada por teste real;
- graceful shutdown requisitado enquanto o subprocess Alembic está rodando é comprovado a: manter o advisory lock até o child terminar, classificar o resultado do child normalmente, e só então liberar o lock e completar shutdown — por teste real PostgreSQL;
- uma segunda tentativa de bootstrap durante essa janela de shutdown-pendente-com-migration-em-voo é comprovada incapaz de iniciar uma nova migration;
- **em nenhum ponto, sob terminação do supervisor (graceful ou hard), duas execuções automáticas de migration são permitidas se sobrepor** por causa do desaparecimento do primeiro supervisor — comprovado por teste que simula a perda do supervisor com o child Alembic ainda ativo e tenta iniciar um segundo bootstrap concorrente;
- o mecanismo escolhido para satisfazer o orphan-safety invariant (propriedade A ou B) é comprovado por teste real: ou (A) o child não sobrevive ao supervisor dono do lock, ou (B) a ownership de serialização permanece viva até o child terminar mesmo com o supervisor original ausente;
- hard termination (kill do processo) é comprovada a não deixar o advisory lock vazado (o próximo processo consegue adquiri-lo) e o próximo processo é comprovado a re-classificar do zero somente depois de ter adquirido legitimamente a proteção de serialização, nunca assumindo completion do attempt anterior;
- suite existente permanece verde.

---

# SM-904 — Operational/deployment documentation integration

## Objetivo

Atualizar a documentação operacional para refletir o novo contrato, sem tocar comportamento de runtime, migrations, Compose, Dockerfile ou workflows — trabalho puramente documental sobre a base já implementada e comprovada por SM-901..SM-903.

## Escopo

Atualizar, com o novo contrato de `DATABASE_MIGRATION_MODE`:

- `README.md`;
- `docs/operations.md`, incluindo a atualização explícita do procedimento de backup/restore para declarar a consequência de `auto` migrar um restore histórico para frente automaticamente, e a recomendação de `verify_only` para inspeção;
- `docs/deployment/easypanel.md`, incluindo revisão de §4 (procedimento de migration hoje baseado em console manual) à luz do novo bootstrap automático;
- `AGENTS.md` e o `CLAUDE.md` correspondente na raiz (mesma disciplina de sincronização já usada por releases anteriores para `/skills`/`/agents`), removendo a afirmação "a aplicação nunca invoca Alembic automaticamente" e substituindo-a pela semântica `auto`/`verify_only`;
- `docs/adr/0011-durable-source-object-storage-s3-and-startup-convergence.md`: apenas uma **nota de forward-reference estreita** apontando para ADR-0015 — nunca reescrita do conteúdo histórico.

### Deployment/static contract integration

Esta task é a dona do invariant estático que substitui, não apaga, `tests/unit/test_deployment_compose.py::test_compose_files_never_auto_run_alembic` (Feature Contract §24) — este item pertence a SM-904 por ser deployment/static contract integration, no mesmo agrupamento das atualizações de documentação acima, não a SM-905 (que é comportamento real de PostgreSQL, concorrência, failure recovery, shutdown critical section e release-image smoke):

- substituir o corpo do teste por um invariante de intenção equivalente: Compose nunca invoca migration Alembic raw/não-serializada diretamente como `command:`/`entrypoint:`; migration automática só é permitida através do caminho de bootstrap sancionado, locked e verificado por ADR-0015 — o teste é **substituído, nunca deletado**;
- nenhuma mudança em `compose.yaml`/`compose.easypanel.yaml`/`compose.portainer.yaml`/Dockerfile é feita por esta task — apenas a asserção de teste estático muda.

Version bump **não** faz parte desta task (SM-906).

## Não fazer

- não alterar `sofias_memory/`, `migrations/`, Compose, Dockerfile ou qualquer workflow;
- não reescrever ADR-0011 além da nota de forward-reference estreita;
- não bump de versão;
- não implementar nenhuma prova de comportamento real de PostgreSQL/concorrência/shutdown para o novo invariant — isso é execução/verificação da suite maior em SM-905, não desta task.

## Gate SM-904

A task só encerra quando:

- os cinco arquivos de documentação listados acima estão consistentes entre si e com o Feature Contract v0.6.0 quanto à spelling/semântica de `DATABASE_MIGRATION_MODE`, comprovado por revisão manual do diff;
- nenhuma afirmação remanescente em qualquer um desses arquivos contradiz "migration pode ser automática em modo `auto`" (busca textual por "nunca automática"/"never automatic"/"manual" ajustada onde aplicável);
- ADR-0011 recebeu apenas a nota de forward-reference, comprovado por diff mínimo e localizado;
- `tests/unit/test_deployment_compose.py::test_compose_files_never_auto_run_alembic` foi **substituído**, não deletado, por um invariante de intenção equivalente (Compose nunca invoca Alembic raw diretamente; automático só via bootstrap sancionado), comprovado pelo teste passando localmente;
- `git diff --stat` confirma zero mudança em código de produção, migrations, Compose, Dockerfile ou workflows;
- suite existente permanece verde (nenhuma suite depende do texto de documentação alterado, mas o gate de lint/format e o teste estático substituído continuam rodando).

---

# SM-905 — Real-PostgreSQL hardening matrix + release-image smoke

## Objetivo

Fechar as obrigações de teste do ADR-0015 (seção "Testing obligations for the future implementation") ainda não cobertas isoladamente por SM-901..SM-903, com infraestrutura real, e provar que Neo4j/worker nunca iniciam cedo demais durante um bootstrap de migration.

## Escopo

### Deployment/static — executado, não definido, aqui

O invariante estático que substitui `tests/unit/test_deployment_compose.py::test_compose_files_never_auto_run_alembic` já foi definido e substituído por SM-904 (deployment/static contract integration). Esta task executa/verifica esse gate como parte da suite completa — não o redefine nem o substitui pela primeira vez aqui.

### Real PostgreSQL (Integration)

- matriz completa combinando os 9 estados observáveis de classificação (SM-901: `PRISTINE_FRESH_SCHEMA`, `UNVERSIONED_NON_EMPTY_SCHEMA`, `EXACT_HEAD`, `KNOWN_ANCESTOR`, `CODE_MULTIPLE_HEADS`, `VERSION_TABLE_EMPTY`, `DATABASE_MULTIPLE_REVISIONS`, `KNOWN_NON_ANCESTOR`, `UNRECOGNIZED_REVISION`) com os dois modos (`auto`/`verify_only`), contra banco real, incluindo os casos ainda não isoladamente cobertos por SM-902/SM-903 — todos fail-closed em ambos os modos onde a tabela do Feature Contract §4 exige, comprovado por teste real; inclui um caso real de `UNRECOGNIZED_REVISION` simulando um rollback genuíno de imagem (revision de uma versão de aplicação futura hipotética, ausente do `ScriptDirectory` corrente), provando fail-closed sem tentar distinguir "futura" de "estrangeira";
- prova explícita de que Neo4j bootstrap e `worker.start()` **nunca** iniciam enquanto o bootstrap de migration está em progresso ou fail-closed — nem durante uma migration em execução, nem durante o estado sticky-failed, nem durante lock-wait;
- prova de que storage convergence (`STORAGE_BACKEND=s3`, ADR-0011) permanece igualmente gated até `OPERATIONAL`.

### Release-image smoke

- smoke de imagem de release cobrindo um banco fresco (pristine) e um banco previamente lançado (ancestral real de uma release anterior), cada um migrando automaticamente até ficar ready sem nenhum passo manual de Alembic;
- confirmação de que a imagem de release não regride o D33 liveness guarantee durante o smoke (`/health/live` responde durante o bootstrap).

### Supervisor-loss / orphan-child serialization scenario (Feature Contract §12, erratum)

Execução, no ambiente de integração suportado, do mecanismo escolhido por SM-903 para satisfazer o orphan-safety invariant, sob perda real do supervisor:

- supervisor desaparece (kill/crash) enquanto o child de migration está ativo → nenhuma migration órfã pode continuar mutando o banco desprotegida;
- um segundo bootstrap, iniciado depois do desaparecimento do supervisor, não consegue começar uma migration concorrente enquanto a migration anterior ainda está ativa ou protegida (propriedade A ou B do erratum, conforme o mecanismo escolhido em SM-903);
- o bootstrap seguinte, uma vez de posse legítima da proteção de serialização, re-lê o estado autoritativo do PostgreSQL do zero — esta task **não exige** que a migration anterior tenha completado, apenas que nenhuma DDL automática concorrente/não-serializada tenha ocorrido.

## Não fazer

- não redesenhar classificação, lock, ou critical section — esta task prova, não redesenha;
- não introduzir nenhum endpoint novo;
- não bump de versão;
- não exigir completion da migration interrompida pela perda do supervisor — a única garantia exigida após hard termination é ausência de DDL automática concorrente/não-serializada.

## Gate SM-905

A task só encerra quando:

- o teste de deployment substituído em SM-904 passa como parte da suite completa executada aqui, e comprovadamente ainda falha se Compose voltar a invocar Alembic raw diretamente;
- a matriz completa dos 9 estados × 2 modos está coberta contra PostgreSQL real, sem gaps relativos à tabela do Feature Contract §4;
- Neo4j/worker never-start-early está comprovado por teste real, não apenas por ausência de código de acionamento;
- o smoke de release image (fresh + previously-released) passa, com D33 preservado durante o bootstrap;
- o cenário de supervisor-loss/orphan-child é comprovado no ambiente de integração suportado: supervisor desaparece com o child ativo → nenhuma migration órfã continua desprotegida → um segundo bootstrap não consegue iniciar migration concorrente → o próximo bootstrap legítimo re-lê o estado autoritativo do zero, sem exigir completion do attempt anterior;
- suite completa (unit/integration/contract/security) permanece verde.

---

# SM-906 — Release prep, quality gates, GATE-v0.6.0

## Objetivo

Fechar o release: quality gates completos, version bump, e o gate formal GATE-v0.6.0 — mesma disciplina de SM-806/v0.5.0.

## Escopo

- Feature Contract `Status` só é promovido de `Proposed` para `Implemented` após os gates abaixo passarem;
- `.github/workflows/integration.yml`: adicionar explicitamente as suítes de integração deste feature (opt-in flags dedicados, mesmo padrão já usado por releases anteriores);
- `CHANGELOG.md` atualizado;
- version bump `APP_VERSION`/canonical version `0.5.0 -> 0.6.0` — somente nesta task, como parte do release commit, nunca antes.

## Quality gate

Executar:

```text
ruff
format/check
mypy
pytest (unit/contract/security/integration)
migration/schema gates (fresh-install e upgrade, sem migration nova
  introduzida por este feature especificamente)
smoke v0.6.0 (SM-905)
runtime-only pip-audit
Bandit HIGH-severity blocking gate
release consistency
```

conforme tooling oficial do repositório. Não mascarar testes existentes, não reduzir cobertura contratual e não excluir suites para obter gate verde.

## GATE-v0.6.0

O release somente pode ser marcado como concluído quando:

- SM-901..SM-905 estiverem aprovadas;
- exatamente a SHA commitada tiver CI verde;
- Integration workflow manual estiver verde contra essa mesma SHA;
- somente então a tag é criada — nunca marcar RELEASED antes de tag + Release workflow, mesma disciplina de v0.5.0;
- `DATABASE_MIGRATION_MODE=auto|verify_only` estiver coerente com o Feature Contract v0.6.0 em toda a documentação;
- o teste de deployment substituído (SM-904, executado como parte da suite completa em SM-905) estiver em vigor, comprovado pelo teste de contrato/estrutural atualizado;
- a matriz de classificação completa (9 estados × 2 modos) estiver comprovada por teste real PostgreSQL;
- a sticky failure semantics e a migration critical section de graceful shutdown estiverem comprovadas por teste real;
- o invariante de orphan-safety de hard termination (Feature Contract §12, erratum) estiver comprovado por teste real de supervisor-loss (SM-903/SM-905) — nenhuma migration desprotegida pode continuar mutando o banco concorrentemente com um novo bootstrap que adquiriu serialização legitimamente;
- Neo4j/worker never-start-early estiver comprovado;
- nenhum endpoint HTTP novo, nenhuma tabela nova para este feature, e nenhuma mudança em Neo4j/`graph_outbox` existirem;
- suite completa estiver verde;
- smoke real estiver verde;
- documentação de v0.6.0 estiver atualizada, incluindo `integration.yml`.

Após esse gate, nenhum trabalho de migration audit table, dedicated migration role, ou rolling-upgrade compatibility deve ser incluído retroativamente no v0.6.0 — esses itens permanecem non-goals explícitos (Feature Contract §27) até um requisito futuro separadamente escopado.

O próximo release funcional planejado permanece separado.
