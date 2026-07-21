#!/bin/bash
# ==============================================================================
# TESTE DE FOGO do vigia_externo.sh (P6) — prova REAL do ciclo KILL -> ressurreição.
#
# Isolado do daemon REAL: usa um LABEL/PATTERN/pulso PRÓPRIOS (com.athena.FIRETEST),
# um "loop" DUBLÊ (python que escreve um pulso VELHO e dorme), e um LaunchAgent
# TEMPORÁRIO com KeepAlive. NUNCA toca no com.athena.local nem nas capturas vivas.
#
# Prova: (1) launchd sobe o dublê (PID1); (2) o vigia vê pulso VELHO + carregado ->
# decide KILL e dá bounce (SIGTERM) no PID1; (3) o KeepAlive do launchd RESSUSCITA
# o dublê (PID2 != PID1). É exatamente o mecanismo que traz a Athena de volta.
# ==============================================================================
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
VIGIA="$HERE/vigia_externo.sh"

LABEL="com.athena.FIRETEST.$$"
MARKER="athena_firetest_LOOP_MARKER_$$"
TMPD="$(mktemp -d)"
PULSO="$TMPD/pulso.json"
LOOPPY="$TMPD/fake_loop.py"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cleanup() {
    launchctl unload "$PLIST" 2>/dev/null
    pkill -f "$MARKER" 2>/dev/null
    rm -f "$PLIST"
    rm -rf "$TMPD"
}
trap cleanup EXIT

# --- dublê do loop: escreve um pulso VELHO (ts no passado) e dorme. Trata SIGTERM
# graciosamente (o bounce manda SIGTERM; o dublê sai e o KeepAlive ressuscita).
cat > "$LOOPPY" <<PY
import json, os, signal, sys, time
pulso = os.environ["FT_PULSO"]
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
# pulso deliberadamente VELHO (3000s atrás) -> o vigia deve decidir KILL.
with open(pulso, "w") as f:
    json.dump({"ts": time.time() - 3000, "ciclo": 1, "ativos": []}, f)
while True:
    time.sleep(1)
PY

# --- LaunchAgent TEMPORÁRIO com KeepAlive (ressuscita o dublê ao morrer).
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>$LOOPPY</string><string>$MARKER</string></array>
  <key>EnvironmentVariables</key><dict><key>FT_PULSO</key><string>$PULSO</string></dict>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLISTEOF

echo "=== TESTE DE FOGO vigia_externo — label=$LABEL ==="
launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST" || { echo "FALHA ao carregar o LaunchAgent"; exit 1; }

wait_pulso() {  # espera o dublê subir E escrever o pulso (timeout 20s). Ecoa o pid.
    local n=0 p=""
    while [ "$n" -lt 40 ]; do
        p="$(pgrep -f "$MARKER" | head -1)"
        [ -n "$p" ] && [ -f "$PULSO" ] && { echo "$p"; return 0; }
        sleep 0.5; n=$((n+1))
    done
    return 1
}

PID1="$(wait_pulso)" || { echo "FALHA: o dublê não subiu / sem pulso"; exit 1; }
# BACKDATA o mtime do pulso para o passado: pulso_idade_s usa max(mtime, ts) (conservador
# anti-falso-positivo), então um arquivo recém-escrito parece FRESCO mesmo com ts velho.
# Um loop VELADO de verdade tem o arquivo parado no passado — é o que reproduzimos aqui.
touch -t 202601010000 "$PULSO"
echo "[1] launchd subiu o dublê: PID1=$PID1 ; pulso backdatado p/ 2026-01-01 (velho)"

# --- roda o vigia contra o dublê (pulso velho + carregado -> KILL). Mudo (sem token).
echo "[2] rodando vigia_externo (STALL_S=900, pulso muito velho) ..."
DECISAO="$(VG_DAEMON_LABEL="$LABEL" VG_DAEMON_PATTERN="$MARKER" VG_PULSO="$PULSO" \
    VG_LOG="$TMPD/vigia.log" VG_STATE="$TMPD/estado" VG_STALL_S=900 VG_KILL_GRACE_S=5 \
    VG_ENV_FILE="$TMPD/env.inexistente" VG_TELEGRAM_TOKEN="" VG_TELEGRAM_CHAT_ID="" \
    bash "$VIGIA")"
echo "    decisão do vigia: $DECISAO"
echo "    --- vigia.log ---"; sed 's/^/    /' "$TMPD/vigia.log"

if [ "$DECISAO" != "KILL" ]; then echo "FALHA: esperava KILL, veio $DECISAO"; exit 1; fi

# --- confirma que PID1 morreu e o KeepAlive ressuscitou (PID2 != PID1).
sleep 1
if kill -0 "$PID1" 2>/dev/null; then echo "FALHA: PID1=$PID1 ainda vivo após o bounce"; exit 1; fi
echo "[3] PID1=$PID1 foi encerrado pelo bounce (SIGTERM). Aguardando ressurreição..."

n=0; PID2=""
while [ "$n" -lt 40 ]; do
    PID2="$(pgrep -f "$MARKER" | head -1)"
    [ -n "$PID2" ] && [ "$PID2" != "$PID1" ] && break
    sleep 0.5; n=$((n+1))
done

if [ -n "$PID2" ] && [ "$PID2" != "$PID1" ]; then
    echo "[4] KeepAlive RESSUSCITOU o dublê: PID2=$PID2 (!= PID1=$PID1)"
    echo "=== TESTE DE FOGO OK: KILL do vigia -> launchd trouxe a Athena de volta. ✔ ==="
    exit 0
fi
echo "FALHA: o dublê não ressuscitou (PID2=$PID2)"; exit 1
