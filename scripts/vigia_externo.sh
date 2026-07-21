#!/bin/bash
# ==============================================================================
# vigia_externo.sh — P6: "quem vigia o vigia" (watchdog EXTERNO da Athena)
# ==============================================================================
#
# O modo de falha que ISTO mata é o que NENHUM watchdog interno consegue matar:
# LIVELOCK do próprio daemon. O watchdog interno (maestro/watchdog.py) roda DENTRO
# do processo do loop — se o loop trava (deadlock, GIL preso, I/O pendurado), o
# watchdog interno trava JUNTO e nunca dispara. Por isso existe um vigia de FORA,
# num processo separado, agendado pelo launchd (StartInterval=300 = a cada 5 min),
# que NÃO compartilha destino com o daemon. Ele olha um único sinal durável — a
# IDADE do heartbeat ~/.athena-local/pulso.json, que o loop toca a cada ciclo — e:
#
#   - pulso VELHO (> 900s) COM daemon carregado no launchd  -> LIVELOCK provado.
#     Manda SIGTERM no processo do LOOP (o python -m maestro.athena_local). O
#     KeepAlive do com.athena.local RESSUSCITA um loop novo; os FILHOS-motor
#     (start_new_session) NÃO morrem com o loop — a captura em andamento sobrevive
#     (Ordem IV: aditivo, nunca mato processo bom). + alerta via curl DIRETO à API
#     do Telegram (canal INDEPENDENTE da Voz do daemon; mesmo token do .env).
#
#   - pulso AUSENTE + LaunchAgent DESCARREGADO -> o dono desligou. SÓ alerta.
#     NUNCA carrego sozinho um daemon que o dono desligou (não sou eu que decido
#     ligar a captura).
#
# BRANCHES conservadoras (a ausência de pulso NÃO é prova de livelock):
#   - pulso FRESCO (<= 900s) + carregado  -> saudável, quieto.
#   - pulso AUSENTE + daemon CARREGADO    -> NÃO mato (pode ser daemon recém-subido
#     ainda sem 1º pulso, ou o seam do heartbeat ainda não fiado no athena_local).
#     Ausência != obsolescência. Registro e aviso o dono (uma vez), mas não ajo.
#
# Cada decisão é logada em ~/.athena-local/vigia_externo.log.
#
# Reversível: launchctl unload + rm do plist. Não toca captura viva a não ser o
# bounce controlado do LOOP (que é justamente o objetivo, e é aditivo aos motores).
#
# Env knobs (defaults = produção; sobrepostos SÓ pelo teste de fogo):
#   VG_ATHENA_HOME     (~/.athena-local)
#   VG_PULSO           ($HOME/pulso.json)
#   VG_LOG             ($HOME/vigia_externo.log)
#   VG_STATE           ($HOME/vigia_estado)          # latch anti-flood entre ticks
#   VG_LOCK_DIR        ($HOME/locks)                 # locks dos motores (prova aditiva)
#   VG_DAEMON_LABEL    (com.athena.local)
#   VG_DAEMON_PATTERN  (maestro[.]athena_local)      # pgrep -f do processo do LOOP
#   VG_STALL_S         (900)
#   VG_ENV_FILE        (/Users/guilhermerodrigues/teste/aula/.env)  # token do Telegram
#   VG_KILL_GRACE_S    (10)                          # espera antes de escalar p/ SIGKILL
#   VG_DRY_RUN         (0)                            # 1 = decide+loga, não mata/alerta
# ==============================================================================
set -u

VG_ATHENA_HOME="${VG_ATHENA_HOME:-$HOME/.athena-local}"
VG_PULSO="${VG_PULSO:-$VG_ATHENA_HOME/pulso.json}"
VG_LOG="${VG_LOG:-$VG_ATHENA_HOME/vigia_externo.log}"
VG_STATE="${VG_STATE:-$VG_ATHENA_HOME/vigia_estado}"
VG_LOCK_DIR="${VG_LOCK_DIR:-$VG_ATHENA_HOME/locks}"
VG_DAEMON_LABEL="${VG_DAEMON_LABEL:-com.athena.local}"
VG_DAEMON_PATTERN="${VG_DAEMON_PATTERN:-maestro[.]athena_local}"
VG_STALL_S="${VG_STALL_S:-900}"
VG_ENV_FILE="${VG_ENV_FILE:-/Users/guilhermerodrigues/teste/aula/.env}"
VG_KILL_GRACE_S="${VG_KILL_GRACE_S:-10}"
VG_DRY_RUN="${VG_DRY_RUN:-0}"

# ------------------------------------------------------------------ log --------
_log() {
    # timestamp ISO + mensagem, append no log. Cria o dir se preciso.
    local dir; dir="$(dirname "$VG_LOG")"
    mkdir -p "$dir" 2>/dev/null
    printf '%s vigia_externo: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$VG_LOG"
}

# ---------------------------------------------------- observação do launchd ----
# Ecoa "1" se o job está carregado no launchd, "0" se não. rc!=0 de `launchctl
# list <label>` => não carregado (launchd imprime "Could not find service").
daemon_carregado() {
    if launchctl list "$VG_DAEMON_LABEL" >/dev/null 2>&1; then echo 1; else echo 0; fi
}

# ------------------------------------------------------- idade do pulso --------
# Ecoa a IDADE do pulso em segundos, ou "-1" se ausente/ilegível. Usa o timestamp
# MAIS FRESCO entre (a) o campo .ts do JSON e (b) o mtime do arquivo — o mais
# conservador contra falso-positivo (se qualquer sinal diz "vivo há pouco", não
# matamos). python3 é o runtime do próprio daemon; se faltar, cai no stat do mtime.
pulso_idade_s() {
    [ -f "$VG_PULSO" ] || { echo -1; return; }
    local agora; agora="$(date +%s)"
    local fresco
    fresco="$(python3 - "$VG_PULSO" <<'PY' 2>/dev/null
import json, os, sys
p = sys.argv[1]
try:
    mtime = os.path.getmtime(p)
except OSError:
    print(""); raise SystemExit
ts = None
try:
    with open(p) as f:
        d = json.load(f)
    v = d.get("ts")
    if isinstance(v, (int, float)):
        ts = float(v)
except Exception:
    ts = None
print(int(max(mtime, ts) if ts is not None else mtime))
PY
)"
    if [ -z "$fresco" ]; then
        # fallback puro-shell: mtime via stat (macOS: -f %m)
        fresco="$(stat -f %m "$VG_PULSO" 2>/dev/null || stat -c %Y "$VG_PULSO" 2>/dev/null)"
    fi
    [ -z "$fresco" ] && { echo -1; return; }
    echo $(( agora - fresco ))
}

# ============================================================ DECISÃO (PURA) ===
# Sem I/O. Lê 3 fatos do ambiente e ecoa EXATAMENTE UM token de ação. Testável em
# tabela, impossível de mentir.
#   VG_DAEMON_LOADED : 1|0
#   VG_PULSO_AGE     : segundos (>=0) ou -1 (ausente)
#   VG_STALL_S       : limiar
# Tokens:
#   KILL         -> carregado + pulso presente + VELHO  (livelock provado)
#   HEALTHY      -> carregado + pulso presente + fresco
#   DOWN_ALERT   -> NÃO carregado (dono desligou; só alerta, nunca ressuscita)
#   NO_HEARTBEAT -> carregado + pulso AUSENTE (sem prova; não mata)
vigia_decidir() {
    local loaded="${VG_DAEMON_LOADED:?}" age="${VG_PULSO_AGE:?}" stall="${VG_STALL_S:-900}"
    if [ "$loaded" != "1" ]; then echo "DOWN_ALERT"; return; fi
    if [ "$age" -lt 0 ]; then echo "NO_HEARTBEAT"; return; fi
    if [ "$age" -gt "$stall" ]; then echo "KILL"; else echo "HEALTHY"; fi
}

# ============================================================== SIDE EFFECTS ===

# Alerta via curl DIRETO à API do Telegram (independente da Voz do daemon).
# Se o token estiver ausente/MUTED, NÃO falha em silêncio: loga que está mudo.
telegram_alerta() {
    local msg="$1" token="" chat=""
    if [ -f "$VG_ENV_FILE" ]; then
        token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$VG_ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\' | tr -d '[:space:]')"
        chat="$(grep -E '^TELEGRAM_CHAT_ID=' "$VG_ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\' | tr -d '[:space:]')"
    fi
    # env sobrepõe o .env (usado pelo teste de fogo p/ apontar a um endpoint mock)
    token="${VG_TELEGRAM_TOKEN:-$token}"
    chat="${VG_TELEGRAM_CHAT_ID:-$chat}"
    local api="${VG_TELEGRAM_API:-https://api.telegram.org}"
    if [ -z "$token" ] || [ "$token" = "MUTED" ] || [ -z "$chat" ]; then
        _log "ALERTA (MUDO — sem TELEGRAM_BOT_TOKEN/CHAT_ID no .env): $msg"
        return 1
    fi
    local http
    http="$(curl -s -o /dev/null -w '%{http_code}' -m 15 \
        -X POST "${api}/bot${token}/sendMessage" \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=${msg}" 2>/dev/null)"
    if [ "$http" = "200" ]; then
        _log "ALERTA entregue (HTTP 200) via curl: $msg"
        return 0
    fi
    _log "ALERTA FALHOU (HTTP ${http:-erro}) via curl: $msg"
    return 1
}

# Só alerta em TRANSIÇÃO de estado (anti-flood: o launchd nos chama a cada 5 min).
# KILL sempre alerta (é uma AÇÃO tomada, e já é raro por natureza — pulso >15min).
_deve_alertar() {
    local decisao="$1" anterior=""
    [ -f "$VG_STATE" ] && anterior="$(cat "$VG_STATE" 2>/dev/null)"
    [ "$decisao" = "KILL" ] && return 0
    [ "$decisao" != "$anterior" ] && return 0
    return 1
}

# Encontra o(s) PID(s) do LOOP (python -m maestro.athena_local). Filtra pelo comm
# = python p/ NÃO pegar o wrapper caffeinate (cujo argv TAMBÉM contém o padrão) e
# nem o próprio vigia. Ecoa os PIDs, um por linha.
_pids_do_loop() {
    local pid comm
    for pid in $(pgrep -f "$VG_DAEMON_PATTERN" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        comm="$(ps -o comm= -p "$pid" 2>/dev/null)"
        case "$comm" in
            *python*|*Python*) echo "$pid" ;;
        esac
    done
}

# Bounce do LOOP: SIGCONT (destrava um processo SIGSTOPado — livelock por congelamento)
# + SIGTERM (saída graciosa; o python trata e flusha). Se sobreviver à graça, escala
# p/ SIGKILL (garante o bounce mesmo num wedge de C-extension). NÃO toca os motores
# (start_new_session -> sessão própria -> sobrevivem; Ordem IV aditivo).
_bounce_loop() {
    local pids; pids="$(_pids_do_loop)"
    if [ -z "$pids" ]; then
        _log "KILL pedido mas NENHUM processo do loop encontrado (padrão=$VG_DAEMON_PATTERN) — nada a matar (anomalia: carregado sem loop?)"
        return 1
    fi
    local pid mortos=""
    for pid in $pids; do
        kill -CONT "$pid" 2>/dev/null
        kill -TERM "$pid" 2>/dev/null
        mortos="$mortos $pid"
    done
    _log "SIGTERM enviado ao(s) loop(s):$mortos (grace ${VG_KILL_GRACE_S}s antes de SIGKILL se preciso)"
    local waited=0
    while [ "$waited" -lt "$VG_KILL_GRACE_S" ]; do
        sleep 1; waited=$((waited+1))
        local vivo=0
        for pid in $pids; do kill -0 "$pid" 2>/dev/null && vivo=1; done
        [ "$vivo" = 0 ] && { _log "loop(s) encerrado(s) graciosamente após ${waited}s (SIGTERM bastou)"; echo "$pids"; return 0; }
    done
    for pid in $pids; do
        if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid" 2>/dev/null; _log "loop $pid resistiu ao SIGTERM (provável wedge/stop) -> SIGKILL"; fi
    done
    echo "$pids"; return 0
}

# ==================================================================== MAIN =====
main() {
    local loaded age decisao
    loaded="$(daemon_carregado)"
    age="$(pulso_idade_s)"

    decisao="$(VG_DAEMON_LOADED="$loaded" VG_PULSO_AGE="$age" VG_STALL_S="$VG_STALL_S" vigia_decidir)"

    case "$decisao" in
        HEALTHY)
            _log "OK — daemon carregado, pulso fresco (${age}s <= ${VG_STALL_S}s). Quieto."
            ;;
        KILL)
            _log "LIVELOCK — daemon CARREGADO mas pulso VELHO (${age}s > ${VG_STALL_S}s). Bounce do loop."
            if [ "$VG_DRY_RUN" = "1" ]; then
                _log "[DRY_RUN] pularia bounce+alerta"
            else
                local morto; morto="$(_bounce_loop)"
                telegram_alerta "Athena vigia-externo: LIVELOCK detectado (pulso ${age}s). Bounce do loop [${morto}] enviado; KeepAlive ressuscita, motores intactos."
            fi
            ;;
        DOWN_ALERT)
            _log "DAEMON DESLIGADO — LaunchAgent ${VG_DAEMON_LABEL} descarregado (pulso $( [ "$age" -lt 0 ] && echo ausente || echo "obsoleto ${age}s" )). NÃO ressuscito (decisão do dono). Só alerto."
            if [ "$VG_DRY_RUN" != "1" ] && _deve_alertar DOWN_ALERT; then
                telegram_alerta "Athena vigia-externo: daemon ${VG_DAEMON_LABEL} DESCARREGADO do launchd. Não ressuscito sozinho (decisão do dono). Se não foi você, recarregue: launchctl load ~/Library/LaunchAgents/${VG_DAEMON_LABEL}.plist"
            fi
            ;;
        NO_HEARTBEAT)
            _log "SEM PROVA — daemon carregado mas pulso AUSENTE. Ausência != obsolescência; NÃO mato (daemon recém-subido? heartbeat não fiado no athena_local?). Só registro."
            if [ "$VG_DRY_RUN" != "1" ] && _deve_alertar NO_HEARTBEAT; then
                telegram_alerta "Athena vigia-externo: daemon ${VG_DAEMON_LABEL} carregado mas SEM pulso (${VG_PULSO}). Não mato sem prova de livelock. Verifique se o heartbeat está sendo escrito."
            fi
            ;;
    esac

    # persiste a decisão para o latch anti-flood do próximo tick
    mkdir -p "$(dirname "$VG_STATE")" 2>/dev/null
    printf '%s' "$decisao" > "$VG_STATE" 2>/dev/null
    echo "$decisao"
}

# Só executa main quando RODADO (não quando SOURCED pelos testes).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
