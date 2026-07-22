# ANEXO F4 — A Athena como supervisora/corretora de SISTEMAS GERADOS

> **Status:** registro DURÁVEL da visão (spec normativa, aditiva) — anexo da
> `2026-07-20-athena-orquestrador-jarvis-spec.md`, materializando o §9 do
> contrato F4 (`aula-2b/docs/superpowers/specs/2026-07-22-f4-integracao-athena-contrato.md`).
> As seções A1–A9 correspondem 1:1 aos itens 1–9 do §9. As seções F4-e/F4-f são
> o DESIGN CRAVADO dos dois passos que faltam do F4 (a implementar sobre as
> branches `f4-core` deste repo e `f4-entrypoint` da fábrica — nunca recriar).
>
> **Código-âncora (LIDO, branch `f4-core` a262dd0, 517 verdes):**
> `maestro/athena_local.py` (`_EspinhaNula` l.146, `_registrar` l.155,
> `_aplicar_decisao_sistema` l.560, `_registrar_desfecho_sistema` l.607,
> `passada_sistema` l.641, `ciclo_local` l.754), `maestro/causa_sistema.py`
> (conjunto FECHADO l.40, exit codes l.32-36), `maestro/decisoes.py`
> (`registrar_decisao` l.75, campo `sistema` l.92/114, `gasto_do_dia` l.261),
> `maestro/adaptadores/sistema.py` (`SistemaExecutor`: lock durável + tee de
> stderr + `drenar_obitos` com exit_code REAL).
> Fábrica (branch `f4-entrypoint` b829da8, 205 verdes):
> `sintetizador/rodar_sistema.py` (exit codes 0/10/20/30/40 FECHADOS),
> `sintetizador/construtor/pipeline.py` (`PedidoConstrucao` l.69, `construir`
> E1→E6 l.131, `ExecutorConstruido` l.80), `sintetizador/construtor/instalar.py`
> (hash fail-closed: módulo editado ≠ manifesto → executor omitido → G4).

---

## A1. Sistemas gerados como ALVOS supervisionados

Um sistema gerado é um alvo ao lado da captura, no MESMO loop (`ciclo_local`,
passo (5)), com as mesmas 6 partes injetadas por null-object aditivo.

- **Registro:** `~/.athena-local/sistemas.yaml` (loader `carregar_sistemas`,
  fail-closed em `slug`/`raiz` ausentes; relido a cada ciclo — sistema novo
  aparece sem restart). Campos: `slug, raiz, estado (ativo|pausado),
  cadencia (sob_demanda|a_cada_horas), entrada_inicial, calibrado_por_tipo,
  teto_dia_usd (default 5.0), precisa_24x7 (só marcador)`.
- **Contrato de ARQUIVOS (SCHEMA_V=1), nunca import:** o repo da Athena JAMAIS
  importa `sintetizador.*` (e vice-versa). A integração é por 4 formatos:
  1. `<raiz>/estado/heartbeat.json` — `{ts, run_id, etapa, pid}` (escritor
     único, atômico); frescor < `ATHENA_SISTEMA_HEARTBEAT_S` (default 900s).
  2. `<raiz>/estado/runs/<run_id>.jsonl` — ledger por-run, append-only,
     leitura TOLERANTE (linha inválida = pulada) implementada do lado Athena.
  3. `<raiz>/estado/resultado-<run_id>.json` — desfecho do run (atômico).
  4. `<raiz>/estado/build-<build_id>.json` — desfecho de uma RECONSTRUÇÃO
     (F4-e, §E.3 abaixo).
- **`SistemaExecutor`** espelha o contrato do `LocalExecutor`
  (`disparar / sistema_ativo / drenar_obitos`), com lock durável
  `locks-sistemas/<slug>.lock` (1 run por slug) e stderr tee'ado desde o dia 1
  (a autópsia de sistema DEPENDE do stderr). Run é subprocesso:
  `ATHENA_FABRICA_PYTHON -m sintetizador.rodar_sistema <raiz> ...`,
  cwd=`ATHENA_FABRICA_DIR`.
- **Diferença dura vs. captura:** sistema NÃO tem sessão de plataforma →
  matar run travado (heartbeat velho) é SEGURO; não existe reseed; anti-ban da
  captura intocado por construção.

## A2. Espinha fiada — contrato NORMATIVO

- A tabela E1–E14 de call-sites do contrato F4 §1.2 é NORMATIVA: remover um
  call-site é regressão (os testes de `test_espinha_fiada.py` têm dentes).
- **Regra de ruído:** só TRANSIÇÃO de estado entra na espinha, nunca estado
  repetido (latches `esgotado_avisado`, `ultimo_erro`, `ultimo_resultado_run`,
  dedup por `(run_id, etapa)`).
- **Campo `sistema`** (`decisoes.py` l.92): slug do sistema a que a
  decisão/custo pertence; `None` = captura/geral ("athena-geral"). Todo
  registro originado da supervisão de sistemas OBRIGA `sistema=<slug>`.
- **Regra inviolável de custo:** `medido=True` (usage real) e `medido=False`
  (proxy/estimativa) JAMAIS são somados num total único — nem na espinha, nem
  no SITREP. (O GATE D5 usa soma conservadora só como comparação de teto —
  §F.2 — nunca como relato.)
- `gasto_do_dia_por_sistema` (§F.1) é o agregador oficial por sistema.

## A3. `causa_sistema` — conjunto-irmão FECHADO

Conjunto (l.40): `{nada, relancar, aguardar_backoff, acionar_engenharia,
pausar_sistema, escalar_token, escalar_humano}`.

- **`escalar_reseed` NÃO EXISTE** — sistema não tem sessão; reseed/login
  automático estão em `_PROIBIDOS` e são RECUSADOS mesmo se o LLM os propuser
  bem-formatados (`_parse_acao_llm`).
- Determinístico-primeiro sobre `(exit_code, stderr_tail)`; contrato de exit
  codes do `rodar_sistema` é sinal FORTE (0/10/20 = normal; 30/40 =
  engenharia); LLM é seam opt-in; desconhecida + sem LLM → `escalar_humano`
  (fail-closed).
- `escalar_token` faz backoff, NUNCA latch (lição do incidente Stoa 21/07).

## A4. Reescrita-com-review — a ÚNICA porta de mudança de código

A Athena NUNCA edita código instalado. Isso não é disciplina, é MECÂNICA: o
hash do manifesto (`instalar.py`) faz módulo editado ≠ manifesto → executor NÃO
carrega → G4 `executor_ausente`. Hot-patch é estruturalmente impossível. A
única porta é re-rodar o Construtor (E1→E6: espec → portão → build TDD
red-first → review POP em loop → smoke REAL + gate F2 → instalar com hash
novo). O fluxo operacional completo está em §E (F4-e). O **disjuntor de
engenharia** (máx. 1 ciclo de engenharia por etapa por dia local; 2ª falha do
MESMO incidente → `escalar_humano`) é parte do contrato: reescrever em loop é
queimar dinheiro.

## A5. Travas D2/D5 operacionais

- **D2 (não-publica):** enforcement no passo 3b do `rodar_sistema` — etapa com
  `tipo_de_resultado` externo-irreversível sem `calibrado_por_tipo[tipo]=true`
  → executor OMITIDO → congela SÓ ela (G4), run termina `congelado_parcial`
  com `trava="irreversivel-externo"` no ledger. **O flip do flag é EXCLUSIVO
  do usuário** (comando/edição do registro, após calibração K=5 do F5); nem a
  Athena nem o entrypoint jamais o escrevem. A Athena distingue esse
  congelamento (não-defeito: alerta 1×/sistema, nenhuma reescrita) de um
  congelamento de engenharia pela `trava` (§E.1).
- **D5 (teto US$5/sistema/dia):** gate no passo (4) da `passada_sistema`,
  ANTES de qualquer `disparar` (run OU build). Modo `ATHENA_BUDGET_MODO`:
  `alerta` (default, ordem do usuário 22/07: registra + alerta, NÃO bloqueia)
  | `pausa` (não dispara). O flip para `pausa` é decisão do usuário. Detalhe
  em §F.2.

## A6. Tabela de gatilhos (quando a Athena age)

| Gatilho | Sinal | Ação |
|---|---|---|
| Reescrever | exit 30/40; traceback; `trava="executor_ausente"`; reprovação esgotada (g2/g4 no ledger) | §E — Construtor, sob disjuntor de engenharia |
| Re-verificar | — | NÃO é decisão da Athena: `reverificar` é ação interna do G3 do runtime; o gate fecha incidente, nunca ela |
| Brainstorm | causa `escalar_humano` (desconhecida) | ANTES de escalar: 1 sessão `claude -p` com autópsia+cauda do ledger+laudo ([[trava-brainstorm-fable]]); saída = ação do conjunto FECHADO (validada) ou "só humano destrava"; custo na espinha `medido=False` |
| Retro-síntese | reprovação ≥50% da etapa nos últimos 3 runs OU custo/dia ≥ 2× média móvel | **só AGENDA em F4** (espinha + SITREP); executar é F6 |
| Escalar | trava dura (budget em modo pausa; token; decisão cravada tocada) | alerta tipado + espinha `tipo="escalada"` |

## A7. SITREP — seção "sistemas gerados"

Por sistema registrado: **estado** (ativo/pausado/pausado_por_causa) · último
run (`run_id`, desfecho, `sucesso`, PROVA = caminho do `resultado.json`/laudo)
· custo do dia **medido × presumido SEPARADOS** (`gasto_do_dia_por_sistema`) ·
decisões/escaladas do dia (espinha filtrada por `sistema`) · pendências
(calibração D2 por tipo; `precisa_24x7` marcado; token). Regra I-1 mantida:
cada linha verificada nos ARQUIVOS, nunca em relato.

## A8. Ponte Telegram (opcional, F4+)

"como está o sistema X?" = resumo do ledger + último laudo com caminho da
prova. "rodar sistema X" = seta `st['pedido_run']` — entra pelo MESMO gate
(disjuntor + D5); jamais um caminho lateral de disparo.

## A9. R3 explícito

Supervisão de sistemas é 100% doméstica (Mac): `sistemas.yaml`, locks,
espinha, runs, builds. O Maestro-VPS não supervisiona sistemas e não ganha
código disso. `precisa_24x7: true` é SÓ marcador de SITREP; migração R1 é
decisão do USUÁRIO (F8). Gatilho que exigir algo só-VPS → escalada, nunca
improviso ([[nunca-sobrepor-decisao-arquitetura]]).

---

## §E. F4-e — GATILHOS DE CORREÇÃO (design cravado, a implementar)

### E.1 Detecção: três fontes, uma classificação

1. **T1 — óbito:** `causa_sistema.classificar` → `acionar_engenharia`
   (exit 30/40, traceback). Ponto de pouso JÁ existe:
   `_aplicar_decisao_sistema` ramo `acionar_engenharia` (athena_local.py
   l.578) — hoje só alerta+espinha; F4-e acrescenta o acionamento (§E.4).
2. **T2 — run `congelado_parcial` (exit 20):** ao observar o
   `resultado.json`, a Athena lê a CAUDA do ledger e classifica pela `trava`
   do último `g4_escalada`/`g2_congelada`:
   `irreversivel-externo` → NÃO-defeito (D2, alerta 1×) · `budget` →
   não-engenharia (re-run só sob disjuntor+D5; reincidente → agendar
   retro-síntese) · `executor_ausente` → engenharia da etapa ·
   `reprovacao_esgotada`/`erro_nao_classificado`/desconhecida → engenharia.
   **Correção obrigatória no código atual:** `_registrar_desfecho_sistema`
   (l.607) hoje rotula TODO congelado como `trava="irreversivel-externo"` —
   passa a rotular pela trava REAL lida do ledger (leitor tolerante local,
   sem import cross-repo).
3. **T3 — reprovação esgotada:** `g2_congelada` com classe
   `reprovacao_esgotada` na cauda → engenharia com o FEEDBACK do último laudo
   reprovado.

Dedup de incidente por `(run_id, etapa)` no `st` do sistema.

### E.2 Disjuntor de engenharia (1 ciclo/etapa/dia)

Estado em `st["eng"][<etapa>] = {"dia": "YYYY-MM-DD", "incidente": run_id,
"builds": n}`. Regras:
- máx. **1 ciclo de engenharia por etapa por dia local** — 2º gatilho no mesmo
  dia NÃO constrói: espinha `tipo="escalada"` + brainstorm-gate → alerta;
- **2ª falha do MESMO incidente** (build pronto → re-run falha de novo com
  causa de engenharia na MESMA etapa) → `escalar_humano` direto (com
  brainstorm-gate antes), nunca 3º build;
- o disjuntor de engenharia é ADICIONAL ao disjuntor comum (backoff) — os dois
  gates valem.

### E.3 A porta de acionamento (integração por ARQUIVO mantida)

A Athena não importa `construir()`. A fábrica ganha um 2º entrypoint fino
(aditivo, espelho do `rodar_sistema`):

```
python -m sintetizador.reconstruir_etapa <raiz> --etapa <id>
       [--feedback feedback.json] [--seed N]
  1. lê construcao/pedidos/<ref>.json — o PedidoConstrucao PERSISTIDO
     (pré-requisito: o E6 do Construtor grava o pedido serializado
     {par, rubrica_ref, caso_smoke, tools, custo_max_usd} ao instalar —
     edição aditiva na fábrica; ausente → exit 20 "pedido não persistido")
  2. consulta DECISOES.md do sistema ANTES: mudança que toque decisão
     cravada → exit 20 (escalada, nunca execução)
  3. anexa o feedback (laudo reprovado + stderr_tail + trava + run_id,
     escrito pela Athena em locks-sistemas/<slug>.feedback.json)
  4. construir(pedido', juiz=juiz-F2, llm, subagente, revisor, rodar_teste)
     — E1→E6 REAL: TDD red-first, review POP em loop, smoke real, gate F2,
     instalar com manifesto/hash NOVOS
  5. escreve <raiz>/estado/build-<build_id>.json ATÔMICO:
     {build_id, etapa, pronto, escalar, motivo, parou_em, hash_modulo,
      custo_medido_usd, custo_presumido_usd, smoke_laudo, ts}
  6. exit: 0 = pronto (instalado) · 20 = escalar (portão/gate barrou;
     esqueleto/versão anterior intactos) · 40 = crash
```

Lado Athena: `SistemaExecutor.disparar_build(slug, etapa, feedback_path)` —
mesmo spawn/lock do slug (**nunca** build e run simultâneos no mesmo sistema),
stderr tee `locks-sistemas/<slug>.build.stderr`.

### E.4 Máquina de estados do incidente (na `passada_sistema`)

```
gatilho de engenharia (E.1, dedup ok, disjuntor de engenharia ok, gate D5 ok)
  → escreve feedback.json → disparar_build → espinha "aciono engenharia"
  → build-<id>.json observado:
      pronto(0)  → espinha custo do build (2 linhas, origem="athena/reescrita")
                   + st["retentar_devido"]=True (re-run sob disjuntor+D5)
      escalar(20)/crash(40) → brainstorm-gate → escalar_humano + espinha
  → re-run: sucesso is True (ou a etapa antes congelada agora aprovada)
      → FECHA o incidente: espinha "reescrita de <etapa> fechou <run_id>"
        (reversivel=True — manifesto/hash anteriores permitem reinstalar)
      → senão: 2ª falha do incidente → E.2 → escalar_humano
```

### E.5 O que entra na espinha (todas com `sistema=<slug>`)

| Transição | Registro |
|---|---|
| gatilho aceito | `o_que="aciono engenharia p/ {slug}:{etapa}"`, `tipo="escalada"`, `trava=<classe>`, `reversivel=True` |
| custo do build | 2 linhas `tipo="custo"` (medido/presumido SEPARADOS, do `build-*.json`), `origem="athena/reescrita"` |
| incidente fechado | `o_que="reescrita de {etapa} fechou incidente {run_id}"`, `reversivel=True` |
| disjuntor de engenharia | `o_que="2º gatilho de engenharia em {etapa} hoje — não construo"`, `tipo="escalada"`, `escalada=True` |
| brainstorm | 1 linha `tipo="custo"` `medido=False` (proxy claude -p) + 1 linha decisão `fonte="llm"` ou escalada "só humano destrava" |

## §F. F4-f — PRESTAÇÃO DE CONTAS (design cravado, a implementar)

### F.1 `gasto_do_dia_por_sistema` (aditivo em `decisoes.py`)

`gasto_do_dia_por_sistema(data=None, dir_base=None) -> dict[str, dict]` —
itera as MESMAS linhas tolerantes de `resumo_do_dia`, agrupa por
`reg.get("sistema") or "athena-geral"`; cada valor:
`{custo_medido_usd, custo_presumido_usd, tokens_in, tokens_out, n_decisoes,
n_escaladas}`. Medido e presumido SEPARADOS sempre (regra inviolável nº2);
custos de build (`origem="athena/reescrita"`) contam no slug — engenharia
gasta o orçamento do sistema.

### F.2 Gate D5 (passo (4) da `passada_sistema`, antes de TODO disparo)

```
g = gasto_do_dia_por_sistema().get(slug, zerado)
conservador = g.medido + g.presumido        # SÓ para comparação de teto
                                            # (mesmo desenho do teto por-run:
                                            # orquestrador._conservador)
se conservador >= spec.teto_dia_usd (default 5.0):
    modo "alerta" (default): espinha (latch 1×/dia/sistema, tipo="escalada",
        trava="budget") + alerta Telegram; DISPARA mesmo assim
    modo "pausa": NÃO dispara (nem run nem build); espinha idem
```

`ATHENA_BUDGET_MODO` ∈ {`alerta`, `pausa`}; default `alerta` (ordem 22/07,
fase infra); o flip é DECISÃO DO USUÁRIO. A soma conservadora existe SÓ dentro
do gate — relato (espinha/SITREP) mantém os dois números separados.

### F.3 /sitrep

Seção "sistemas gerados" conforme A7, alimentada por
`gasto_do_dia_por_sistema` + `resumo_do_dia` (filtro `sistema`) + os arquivos
de estado dos sistemas (I-1: prova em arquivo, não relato).

## §V. Critérios de verificação (F4-e/F4-f)

1. **Correção fim-a-fim (dublê + 1 smoke real):** executor sabotado → run
   reprova (g2/g4) → Athena escreve `feedback.json` + dispara
   `reconstruir_etapa` → `build-*.json` pronto → re-run fecha o incidente; a
   espinha contém a cadeia completa (gatilho → custo do build → fechamento).
2. **Prova negativa do hash:** editar `executor.py` instalado na mão → próximo
   run congela em G4 → a Athena classifica `executor_ausente` → engenharia via
   Construtor. A porta lateral NÃO existe.
3. **Disjuntor de engenharia com dentes:** 2º gatilho na mesma etapa no mesmo
   dia → NENHUM build disparado; espinha registra a escalada. 2ª falha do
   mesmo incidente → `escalar_humano`, nunca 3º build.
4. **Trava real no desfecho:** run congelado por D2 → `trava=
   "irreversivel-externo"` e NENHUMA reescrita; congelado por
   `executor_ausente` → engenharia (a regressão do rótulo fixo atual é
   corrigida e testada com as duas caudas de ledger).
5. **D5:** fixture com custo ≥ teto → modo `alerta` registra+alerta E dispara;
   `ATHENA_BUDGET_MODO=pausa` → não dispara; latch de 1 alerta/dia/sistema.
6. **`gasto_do_dia_por_sistema`:** linhas mistas (`sistema=None` e slugs) →
   agrupamento correto; medido≠presumido nunca somados no retorno.
7. **Zero regressão:** 517 (maestro) + 205 (sintetizador) verdes; com
   `_EspinhaNula` e sem sistemas registrados o comportamento é o
   pré-integração (padrão null-object mantido).

## §D. Decisões que SÓ O USUÁRIO crava

1. **Merge/ativação no daemon vivo** — ORDEM IV: nenhum restart forçado; o
   código novo ativa no próximo restart NATURAL. O merge de
   `f4-core`/`f4-entrypoint` em main e a ativação são aprovação dele.
2. **Modo do gate D5:** manter `alerta` (default proposto) ou ligar `pausa`.
3. **Cadência default:** `sob_demanda` (proposto — cada run custa) ou agendada.
4. **Flip de `calibrado_por_tipo`:** sempre dele, após K=5 (F5).
5. **Migração R1 (24/7 na VPS):** só ele, quando um sistema real exigir (F8).
