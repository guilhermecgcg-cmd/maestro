#!/bin/bash
# Testes do vigia_externo.sh. Foco: a função PURA vigia_decidir (tabela) + o latch
# anti-flood _deve_alertar + o caminho MUDO do telegram_alerta. Roda sem bats:
#   bash tests/test_vigia_externo.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/vigia_externo.sh"

# Sourceia as funções sem executar main (o guard BASH_SOURCE cuida disso).
# shellcheck disable=SC1090
source "$SCRIPT"

fails=0
pass=0
check() {  # check <descricao> <esperado> <obtido>
    if [ "$2" = "$3" ]; then pass=$((pass+1));
    else fails=$((fails+1)); printf 'FAIL: %s\n  esperado=[%s] obtido=[%s]\n' "$1" "$2" "$3"; fi
}

decidir() {  # decidir <loaded> <age> [stall]
    VG_DAEMON_LOADED="$1" VG_PULSO_AGE="$2" VG_STALL_S="${3:-900}" vigia_decidir
}

# ---- Tabela da decisão pura -------------------------------------------------
# carregado + pulso velho -> KILL (o coração do vigia: livelock provado)
check "loaded+velho(901>900)"        KILL         "$(decidir 1 901 900)"
check "loaded+muito velho(5000)"     KILL         "$(decidir 1 5000 900)"
# fronteira exata: idade == limiar NÃO mata (só > mata)
check "loaded+idade==limiar(900)"    HEALTHY      "$(decidir 1 900 900)"
check "loaded+fresco(120)"           HEALTHY      "$(decidir 1 120 900)"
check "loaded+fresco(0)"             HEALTHY      "$(decidir 1 0 900)"
# NÃO carregado -> nunca mata, só alerta (mesmo com pulso obsoleto de sobra)
check "descarregado+pulso ausente"   DOWN_ALERT   "$(decidir 0 -1 900)"
check "descarregado+pulso obsoleto"  DOWN_ALERT   "$(decidir 0 9999 900)"
check "descarregado+pulso fresco"    DOWN_ALERT   "$(decidir 0 5 900)"
# carregado + SEM pulso -> NÃO mata (ausência != obsolescência)
check "loaded+pulso ausente"         NO_HEARTBEAT "$(decidir 1 -1 900)"
# limiar customizado respeitado
check "stall custom 60: age 61"      KILL         "$(decidir 1 61 60)"
check "stall custom 60: age 60"      HEALTHY      "$(decidir 1 60 60)"

# ---- Latch anti-flood (_deve_alertar) --------------------------------------
TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
VG_STATE="$TMPD/estado"
# KILL sempre alerta, mesmo repetido
printf 'KILL' > "$VG_STATE"
if _deve_alertar KILL; then check "KILL sempre alerta (repetido)" ok ok; else check "KILL sempre alerta (repetido)" ok NAO; fi
# DOWN_ALERT: 1ª vez (estado anterior != DOWN_ALERT) alerta
printf 'HEALTHY' > "$VG_STATE"
if _deve_alertar DOWN_ALERT; then check "DOWN_ALERT em transicao alerta" ok ok; else check "DOWN_ALERT em transicao alerta" ok NAO; fi
# DOWN_ALERT repetido (estado já DOWN_ALERT) NÃO re-alerta (anti-flood)
printf 'DOWN_ALERT' > "$VG_STATE"
if _deve_alertar DOWN_ALERT; then check "DOWN_ALERT repetido NAO alerta" ok NAO; else check "DOWN_ALERT repetido NAO alerta" ok ok; fi

# ---- telegram_alerta MUDO não estoura e loga (sem token) --------------------
VG_LOG="$TMPD/log"; VG_ENV_FILE="$TMPD/env.inexistente"; VG_TELEGRAM_TOKEN=""; VG_TELEGRAM_CHAT_ID=""
if telegram_alerta "teste mudo"; then muted_rc=0; else muted_rc=1; fi
check "telegram MUDO retorna !=0" 1 "$muted_rc"
grep -q "MUDO" "$TMPD/log" && check "telegram MUDO loga honesto" ok ok || check "telegram MUDO loga honesto" ok NAO

# =============================================================================
# fix-livelock-loop — FALSO LIVELOCK. Evidência (vigia_externo.log x wtmp x pmset):
# 123 de 130 KILLs vieram DEPOIS de o próprio vigia ficar 21min..7,5d sem rodar (Mac
# dormindo de tampa fechada na bateria / desligado / sem login); o do 10/09 16:29 matou
# um loop de ~20s de vida (nascido no login) por um pulso de 6 dias; 07/08 11:32-11:52
# matou 4 loops recém-nascidos seguidos. Dois fatos novos entram na decisão:
#   VG_DESDE_ACORDAR : s desde o último wake do Mac (kern.waketime), -1 = desconhecido
#   VG_LOOP_IDADE    : s de vida do processo de loop MAIS NOVO,     -1 = desconhecido
# =============================================================================
decidir2() {  # decidir2 <loaded> <age> <desde_acordar> <loop_idade> [stall]
    VG_DAEMON_LOADED="$1" VG_PULSO_AGE="$2" VG_DESDE_ACORDAR="$3" VG_LOOP_IDADE="$4" \
        VG_STALL_S="${5:-900}" vigia_decidir
}
# o caso do 10/09 16:29: pulso 545295s, Mac acordado há 14min, loop com 20s -> NÃO mata
check "10/09: pulso 6d, acordou ha 870s"      SONO     "$(decidir2 1 545295 870 20)"
check "10/09 sem wake: loop de 20s"           NASCENTE "$(decidir2 1 545295 -1 20)"
# 07/08 11:37: loop nascido há 4,5min (reiniciado pelo KILL anterior), Mac acordado há horas
check "07/08: loop de 270s, acordado ha 3h"   NASCENTE "$(decidir2 1 1407 10800 270)"
# 03/09 09:01: acordou após 7,5 dias dormindo; loop antigo (congelado pelo SO)
check "03/09: acordou ha 5s, loop velho"      SONO     "$(decidir2 1 644251 5 900000)"
# LIVELOCK REAL continua morto: acordado há muito, loop velho, pulso velho
check "livelock real acordado"                KILL     "$(decidir2 1 5000 10800 10800)"
check "fronteira: acordou ha 901s"            KILL     "$(decidir2 1 5000 901 10800)"
check "fronteira: acordou ha 900s"            SONO     "$(decidir2 1 5000 900 10800)"
check "fronteira: loop com 901s"              KILL     "$(decidir2 1 5000 -1 901)"
check "fronteira: loop com 900s"              NASCENTE "$(decidir2 1 5000 -1 900)"
# fatos desconhecidos (-1) = comportamento ANTIGO (retrocompat da tabela de cima)
check "desconhecidos -> KILL antigo"          KILL     "$(decidir2 1 901 -1 -1)"
check "pulso fresco ignora o resto"           HEALTHY  "$(decidir2 1 120 5 5)"
check "descarregado nunca vira SONO"          DOWN_ALERT "$(decidir2 0 9999 5 5)"
check "sem pulso nunca vira NASCENTE"         NO_HEARTBEAT "$(decidir2 1 -1 5 5)"
# graças configuráveis (o teste de fogo zera a de nascente p/ exercitar o KILL real)
check "graca nascente 0: loop 1s"             KILL \
    "$(VG_GRACA_NASCENTE_S=0 decidir2 1 5000 -1 1)"
check "graca sono 0: acordou ha 1s"           KILL \
    "$(VG_GRACA_SONO_S=0 decidir2 1 5000 1 10800)"

# ---- CRASH-LOOP: a graça NASCENTE não pode calar um daemon que reinicia sem pulsar ----
# (antes do fix esse modo de falha ao menos virava KILL+alerta a cada 5 min; sem esta
# regra ele viraria NASCENTE mudo para sempre)
decidir3() {  # decidir3 <estado_anterior> <pid_anterior> <loop_pid>
    VG_DAEMON_LOADED=1 VG_PULSO_AGE=5000 VG_DESDE_ACORDAR=-1 VG_LOOP_IDADE=20 \
        VG_ESTADO_ANTERIOR="$1" VG_PID_ANTERIOR="$2" VG_LOOP_PID="$3" VG_STALL_S=900 \
        vigia_decidir
}
check "nascente de novo, OUTRO pid -> CRASHLOOP"  CRASHLOOP "$(decidir3 NASCENTE 111 222)"
check "crashloop persiste com outro pid"          CRASHLOOP "$(decidir3 CRASHLOOP 222 333)"
check "nascente de novo, MESMO pid -> NASCENTE"   NASCENTE  "$(decidir3 NASCENTE 111 111)"
check "1a nascente (antes HEALTHY) -> NASCENTE"   NASCENTE  "$(decidir3 HEALTHY 111 222)"
check "pid anterior desconhecido -> NASCENTE"     NASCENTE  "$(decidir3 NASCENTE '-' 222)"
check "sem estado anterior -> NASCENTE"           NASCENTE  "$(decidir3 '' '' 222)"

# ---- parsers ------------------------------------------------------------------
check "etime mm:ss"            20      "$(_etime_para_s '00:20')"
check "etime 4:31"             271     "$(_etime_para_s '04:31')"
check "etime octal-trap 08:09" 489     "$(_etime_para_s '08:09')"
check "etime hh:mm:ss"         3723    "$(_etime_para_s '01:02:03')"
check "etime d-hh:mm:ss"       535560  "$(_etime_para_s '6-04:46:00')"
check "etime com espacos"      725     "$(_etime_para_s '   12:05')"
check "etime vazio"            -1      "$(_etime_para_s '')"
check "etime lixo"             -1      "$(_etime_para_s 'abc')"
check "etime so segundos"      -1      "$(_etime_para_s '42')"
check "waketime sysctl"        1789071287 \
    "$(_parse_waketime '{ sec = 1789071287, usec = 206221 } Thu Sep 10 16:14:47 2026')"
check "waketime lixo"          ""      "$(_parse_waketime 'nada aqui')"
agora_t="$(date +%s)"
d="$(VG_WAKETIME=$((agora_t - 42)) desde_acordar_s)"
if [ "$d" -ge 41 ] && [ "$d" -le 45 ]; then check "desde_acordar ~42s" ok ok; else check "desde_acordar ~42s" ok "$d"; fi
check "waketime 0 -> desconhecido"      -1 "$(VG_WAKETIME=0 desde_acordar_s)"
check "waketime futuro -> desconhecido" -1 "$(VG_WAKETIME=$((agora_t + 999)) desde_acordar_s)"
check "waketime lixo -> desconhecido"   -1 "$(VG_WAKETIME=abc desde_acordar_s)"
d="$(desde_acordar_s)"   # sysctl REAL desta máquina (só leitura): tem de ser número >= -1
case "$d" in -1|[0-9]*) check "desde_acordar real e numerico" ok ok ;; *) check "desde_acordar real e numerico" ok "$d" ;; esac

# ---- ponta-a-ponta do main (SEM launchd, SEM o daemon real) --------------------
# Um "loop" DUBLÊ (python com um MARCADOR único no argv — o padrão do vigia só casa ELE) e
# um pulso VELHO com fase/curso. daemon_carregado é sobreposto (nada de launchctl).
PY="$(command -v python3)"
MARK="athena_vigia_e2e_MARKER_$$_$RANDOM"
"$PY" -c 'import time,sys; time.sleep(120)' "$MARK" &
FAKE=$!
sleep 1.2                                      # etime >= 1s
PULSO_T="$TMPD/pulso.json"
printf '{"ts": %s, "ciclo": 7, "ativos": [], "pid": 4242, "fase": "passada", "curso": "https://x/c2"}' \
    "$(( $(date +%s) - 3000 ))" > "$PULSO_T"
touch -t 202601010000 "$PULSO_T"               # mtime velho também (max(mtime,ts) velho)
e2e() {  # e2e <graca_nascente> -> decisão
    # SEGURANÇA DO VIVO: roda num bash NOVO com TODO caminho e o PADRÃO do loop fixados no
    # ambiente ANTES do source — o default `maestro[.]athena_local` (o daemon REAL) nunca
    # chega a existir lá dentro. Guarda dura: sem o marcador do dublê, aborta sem agir.
    env VG_ATHENA_HOME="$TMPD/home" VG_PULSO="$PULSO_T" VG_LOG="$TMPD/e2e.log" \
        VG_STATE="$TMPD/e2e.estado" VG_LOCK_DIR="$TMPD/home/locks" \
        VG_DAEMON_LABEL="com.athena.E2E.naoexiste" VG_DAEMON_PATTERN="$MARK" \
        VG_STALL_S=900 VG_KILL_GRACE_S=3 VG_WAKETIME=0 VG_GRACA_NASCENTE_S="$1" \
        VG_ENV_FILE="$TMPD/env.inexistente" VG_TELEGRAM_TOKEN="" VG_TELEGRAM_CHAT_ID="" \
        /bin/bash -c '
            source "$1"
            case "$VG_DAEMON_PATTERN" in
                athena_vigia_e2e_MARKER_*) ;;
                *) echo "ABORT-padrao-inseguro"; exit 99 ;;
            esac
            daemon_carregado() { echo 1; }   # dublê: "carregado" sem tocar o launchd
            main' _ "$SCRIPT"
}
check "e2e: loop recém-nascido NÃO é morto" NASCENTE "$(e2e 900)"
if kill -0 "$FAKE" 2>/dev/null; then check "e2e: dublê segue VIVO" ok ok; else check "e2e: dublê segue VIVO" ok MORTO; fi
grep -q "NASCENTE" "$TMPD/e2e.log" && check "e2e: log honesto NASCENTE" ok ok || check "e2e: log honesto NASCENTE" ok NAO
# o dublê "crasha" e o launchd sobe OUTRO (novo pid), ainda sem pulso -> CRASHLOOP, sem kill
kill -9 "$FAKE" 2>/dev/null; wait "$FAKE" 2>/dev/null
"$PY" -c 'import time,sys; time.sleep(120)' "$MARK" &
FAKE=$!
sleep 1.2
check "e2e: renasceu com OUTRO pid e sem pulso -> CRASHLOOP" CRASHLOOP "$(e2e 900)"
if kill -0 "$FAKE" 2>/dev/null; then check "e2e: CRASHLOOP não mata" ok ok; else check "e2e: CRASHLOOP não mata" ok MORTO; fi
grep -q "CRASH-LOOP" "$TMPD/e2e.log" && check "e2e: log honesto CRASH-LOOP" ok ok || check "e2e: log honesto CRASH-LOOP" ok NAO
check "e2e: sem graça -> KILL (livelock real)" KILL "$(e2e 0)"
sleep 0.5
if kill -0 "$FAKE" 2>/dev/null; then check "e2e: dublê MORTO pelo KILL" ok VIVO; kill -9 "$FAKE" 2>/dev/null; else check "e2e: dublê MORTO pelo KILL" ok ok; fi
# só a linha do KILL (a do NASCENTE, acima no mesmo log, também traz o contexto)
grep "LIVELOCK" "$TMPD/e2e.log" | grep -q "fase=passada curso=https://x/c2 ciclo=7 pid=4242" \
    && check "e2e: KILL diz ONDE parou" ok ok || check "e2e: KILL diz ONDE parou" ok NAO
wait "$FAKE" 2>/dev/null

echo "-----------------------------------------"
echo "PASS=$pass FAIL=$fails"
[ "$fails" -eq 0 ]
