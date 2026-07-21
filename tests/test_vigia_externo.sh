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

echo "-----------------------------------------"
echo "PASS=$pass FAIL=$fails"
[ "$fails" -eq 0 ]
