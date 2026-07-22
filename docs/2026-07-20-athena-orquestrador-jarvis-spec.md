# Athena — Spec COMPLETA do Orquestrador (Jarvis)

**Data:** 2026-07-20 · **Status:** spec integral (não por etapa)
**Objetivo:** a Athena SUBSTITUI o humano no processo de captura de conhecimento — recebe o link, entende o curso, dispara os sistemas certos, corrige desvios, prioriza, e só chama o humano no Telegram quando precisa de DECISÃO dele. Não é watchdog; é o cérebro que assume o lugar do humano.

> **ANEXO F4 (22/07, normativo):** a Athena também supervisiona, corrige e
> presta contas de SISTEMAS GERADOS pela fábrica — registro, espinha como
> contrato, `causa_sistema`, reescrita-com-review (única porta = Construtor),
> travas D2/D5, gatilhos, seção "sistemas" no SITREP e R3 explícito:
> `docs/2026-07-22-athena-sistemas-anexo-f4.md`.

## 0. Diagnóstico honesto (por que esta spec existe)
A Athena F0 (o que EXISTE) é um watchdog com 4 ações: `{restart, redeploy, reenqueue, nada}`. Detecta+avisa+reinicia (reiniciou o painel-web sozinha), mas: **não vê progresso real** (lê flags, não o Notion), **não orquestra as passadas** (deixa curso parcial), **não conserta código** (escala o bug pro humano), **não previne o falso-pronto**, **não prioriza**. O humano ainda é o orquestrador. Esta spec define TUDO que falta pra ela assumir.

## 1. Os 3 problemas que a arquitetura resolve
- **P1 — Claude é cego na VPS.** Executa um comando, lê o output daquele instante, reporta; se o processo cai 5s depois, não sabe. → **Camada 1: observador externo que enxerga o estado REAL, contínuo, com timestamp.**
- **P2 — O humano está no lugar do orquestrador.** Abre terminais, copia outputs, decide. → **Camada 2: a Athena assume — lê estado, compara com o esperado, decide (corrige ou escala).**
- **P3 — Sem feedback loop, o agente não corrige rota.** Sem confirmação de sucesso/falha, não aprende. → **Feedback real por ação, verificado contra a fonte de verdade (não o output do instante).**

## 2. Arquitetura em 3 camadas

### Camada 1 — Observabilidade (os olhos) — processo 24/7 na VPS
A cada X min: GET /health em cada serviço · verifica portas (socket) · lê status dos containers (docker ps) / PM2 (jlist) · checa a fila (fila_captura no Postgres) · **checa o PROGRESSO REAL da captura contra o Notion** (done/total por curso e por TIPO de aula, nunca flags) · grava estado+timestamp num store durável (Postgres/Supabase). Sem isso, qualquer cérebro é cego (P1).

### Camada 2 — Orquestrador (o cérebro) — a Athena
Lê o estado da Camada 1 → compara com o ESPERADO (serviços + plano de captura) → **decide**: corrige sozinha (restart/redeploy/reenqueue/disparar a passada certa/disparar o braço não-vídeo/pedir re-injeção de sessão) OU escala (só decisão real). Usa Claude Code como EXECUTOR — com **feedback real (P3)**: cada ação confirma sucesso/falha na fonte de verdade. **Auto-reparo (F3):** ao detectar bug de código, gera sub-agente que escreve+testa(dentes)+deploya+verifica o fix — poderes limitados (nunca deletar, nunca logar, review antes de deploy).

### Camada 3 — Interface de controle
**Telegram Bot:** notifica SÓ quando precisa de decisão; aceita comandos (`/status`, `/restart projeto-x`, `/capturar <link>`, `/prioridade`). **Frontend Jarvis (Next.js dark):** painel em tempo real (a galáxia já existe) — serviços, progresso da captura, fila, alertas.

## 3. A Athena como CABEÇA da captura (o fluxo que hoje o humano faz na mão)
1. **Recebe o link** (Telegram / cadastro / cursos_desejados).
2. **Anti-duplicidade (trava 1):** consulta o Notion por Origem — se já capturado, pula. [CONSTRUÍDO: branch `athena-antiduplicidade`.]
3. **Inspeciona o curso:** enumera — quantas aulas, que TIPO (legenda / sem-legenda / Vimeo / documento / texto / vazia). [Classificador uma-abertura.]
4. **Dispara o sistema certo por aula, até 100% (medido no Notion):** legenda→WebVTT · sem-legenda→áudio/Whisper · Vimeo→embed HLS · documento→download+anexo · texto→replica. Aciona cada passada quando necessário.
5. **Entende o anti-ban:** cadência humana · 1 curso/vez · nunca loga · **disjuntor que separa "parede real" de "aula não-vídeo"** (mata o falso-pronto) · sessão residencial (Mac) · pré-voo (sessão viva? Chrome ok? URL é de produto?).
6. **Aciona o Sintetizador:** curso how-to → gera skill + agente + sistema executável.
7. **Verifica (Auditor):** completude por Notion, não por flag; rejeita "pronto" não-verificado (I-1).
8. **Reporta:** atualiza a tabela de controle; Telegram só se precisa de decisão.

## 4. Spec de COMPORTAMENTO da Athena (derivada do PADRÃO do humano nesta sessão)
Como o humano decide, pra a Athena decidir como ele decidiria:

**Prioridade INVIOLÁVEL: QUALIDADE > TEMPO > RECURSOS.** Sempre.

**O que o humano ODEIA → a Athena NUNCA faz:**
- Falso-pronto / desonestidade (afirmar "concluído" sem verificar — pegou "Invisto 65/537 concluído" e "pronto" com 466 pendentes). → **nunca afirma done sem provar no Notion.**
- Ser perguntado o óbvio / trabalho dividido pra parar no meio. → **executa a sequência inteira; só escala decisão REAL.**
- Preguiça / evitar trabalho. → **nada impede de trabalhar; DUAS FRENTES sempre (captura + infra).**
- Ler a fonte errada (tracker.db/flag em vez do Notion). → **fonte de verdade = Notion, sempre.**
- Duplicidade. → **tripla-trava (Athena checa Notion → motor checa Origem → fonte = Notion).**
- Sobrepor decisão de arquitetura dele. → **decisão de arquitetura do humano é inviolável; tradeoff → escala.**

**O que o humano QUER → a Athena persegue:**
- Ele SUBSTITUÍDO no loop de captura (a Athena é a cabeça).
- Controle absoluto (rastreamento por curso, % que falta, sempre atualizado no Notion).
- Proatividade (agir até concluir, não narrar opções).
- Honestidade brutal (verificado vs presumido, sempre distinguido).
- Anti-ban protegendo a conta paga como inviolável.

**Escalonamento (quando chamar no Telegram):** SÓ decisão real — design/direção/arquitetura · ação irreversível/paga · credencial (a Athena NUNCA digita) · anti-ban aceso (esperar é decisão). **NUNCA:** confirmar o óbvio, narrar sem entregar, perguntar "o que primeiro".

## 5. Feedback loop (P3, detalhado)
Cada ação: executa → **verifica o resultado na fonte de verdade** (Notion p/ captura, observador p/ serviços), não no output do instante → sucesso: registra+segue; falha: diagnostica (Cérebro/LLM) → corrige (whitelist ampliada + auto-reparo) ou escala → grava no store (auditoria + aprendizado).

## 6. Sub-agentes + delegação
Cada projeto/sistema tem seu sub-agente (captura, sintetizador, adaptador-X, frontend). O orquestrador "chibateia" via chamadas de API internas (o painel-api já é a costura — ampliar). A Athena delega, monitora resultados (feedback real), corrige desvios.

## 7. Roadmap F0→F4 (built vs not)
- **F0 Vigia** (🟢 no ar): observação básica + 4 ações ops + reconcile (bug corrigido hoje) + Auditor (não plugado) + enfileiramento (sem trava Notion — corrigido no branch).
- **F1 Capturadora Completa** (🔴): Camada 1 observador + visão-por-Notion + orquestração das 3 passadas até 100% + disjuntor inteligente + Auditor plugado + anti-duplicidade (⚪ branch pronto).
- **F2 Multi-Plataforma** (🔴): adaptadores coordenados.
- **F3 Auto-Reparo** (🔴): diagnostica→escreve fix→testa→deploya→verifica + cria agentes. A lacuna central.
- **F4 Auto-Evolução** (🔴): melhora o próprio código, prioriza o roadmap.

## 8. Critérios de "a Athena me substituiu"
1. Mando um link no Telegram → ela captura o curso inteiro (todas as passadas, 100% no Notion) sem eu tocar em nada.
2. Um serviço cai / um bug aparece → ela conserta (ops OU escreve+deploya o fix) sem eu saber.
3. Ela decide prioridades e só me chama pra decisão real.
4. Zero falso-pronto: tudo que ela diz "feito" está provado no Notion.
5. Anti-ban nunca violado; conta paga protegida.

---
# ADENDO (20/07) — Decisões + Capacidades autônomas exigidas

## Decisão de stack (aprovada)
A Athena é construída em **Python/Docker/Postgres, reusando o maestro** (acesso.py já tem docker/health/Postgres; observador, anti-dup e reconcile já nesse stack). A Camada 3 (painel) usa o Next.js/r3f já existente. Nada do maestro é jogado fora.

## Capacidade A — Portão de Qualidade POP (automatizado, NÃO-OPCIONAL)
Toda coisa nova que a Athena produz (função, sistema, agente, skill, adaptador) passa OBRIGATORIAMENTE por:
```
ANTES do spec:  code-review do código existente → corrige TODOS os bugs (não só graves) → code-review de novo → repete até zerar → SÓ ENTÃO escreve o spec
DEPOIS da impl: code-review da implementação → corrige TODOS os bugs → code-review de novo → repete até zerar → só então declara pronto (verificado no Notion, I-1)
```
Isto é uma CAPACIDADE da Athena, não um processo meu: ela **dispara os agentes de review e de fix sozinha**, em loop, e não avança enquanto sobrar bug. Regras do loop (invioláveis): verificar cada achado contra o código; teste-com-dentes obrigatório (reintroduz o bug, confirma que o teste pega); dublê que modela o mecanismo; "corrige TODOS os bugs" (o usuário rejeitou explicitamente 'só os graves').

## Capacidade B — Criação autônoma de adaptadores de plataforma
Dada a URL de uma plataforma nova (Kiwify, Nutror, Alpaclass, Kajabi, Hubla, Greenn, Stoa, Entrega Digital, domínio próprio…), a Athena:
1. **Recon autenticado** (sessão residencial do Mac): identifica a plataforma, o player de vídeo, o mecanismo de listagem de aulas, o esquema de sessão/anti-ban.
2. **Passa pelo Portão A** (review do recon + do código-base do motor onde o adaptador encaixa).
3. **Escreve o spec do adaptador** → **plano** → **build TDD** (subagent-driven) → **Portão A de novo** (review→fix TODOS→review).
4. **Deploy + validação ao vivo** (I-1: prova no Notion) antes de declarar pronto.
5. O pipeline downstream é agnóstico → o adaptador só entra na boca de entrada.

## Capacidade C — Operar o Sintetizador + gerar o sistema de exibição/execução
Para curso how-to: a Athena aciona o Sintetizador (curso → **skill + agente + sistema executável**) e, além disso, **cria do zero o sistema que EXIBE/EXECUTA o que o curso ensina** (o resultado prático da metodologia). Cada sistema gerado passa pelo Portão A. O sistema de exibição usa o stack do frontend (Next.js) e é conectado à camada de conhecimento (busca semântica) como fonte.

## Critério de aceite desta fase
A Athena, sozinha: recebe link → não-duplica → inspeciona → captura completa (todas as passadas, 100% no Notion) → opera sintetizador nos how-to → gera o sistema de exibição → e, quando encontra plataforma nova ou bug, **constrói o adaptador/fix passando pelo Portão A** — chamando o humano só para decisão real. Tudo verificado no Notion, nunca por flag.
