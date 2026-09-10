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
#   VG_WAKETIME        ("")    # epoch do último wake; vazio = lê `sysctl kern.waketime`
#   VG_GRACA_SONO_S    (=VG_STALL_S)  # carência após o Mac ACORDAR
#   VG_GRACA_NASCENTE_S (=VG_STALL_S) # carência de um loop RECÉM-NASCIDO
#
# FALSO LIVELOCK (fix-livelock-loop, evidência em vigia_externo.log x wtmp x pmset):
# 123 de 130 KILLs vieram logo DEPOIS de o próprio vigia ficar 21min..7,5d sem rodar —
# o Mac estava DORMINDO (tampa fechada, bateria; o caffeinate -is não segura sono de tampa
# e o -s só vale na tomada) ou DESLIGADO/sem login (LaunchAgent só roda com sessão). O
# pulso envelhece no RELÓGIO DE PAREDE enquanto o loop está CONGELADO pelo SO (o
# time.monotonic do loop não anda dormindo). Bouncear aí não destrava nada — só zera o
# estado em memória (backoff do disjuntor, latches) e manda alerta enganoso. E um loop
# RECÉM-NASCIDO herda o pulso da encarnação ANTERIOR: 07/08 11:32-11:52 matou 4 loops
# novos seguidos, presos no 1º curso do 1º ciclo; 10/09 16:29 matou um de ~20s de vida.
# Por isso, com pulso velho, o vigia ESPERA (não mata) em dois casos:
#   SONO     — o Mac acordou há <= VG_GRACA_SONO_S (kern.waketime): o loop mal teve CPU.
#   NASCENTE — o loop mais novo tem <= VG_GRACA_NASCENTE_S de vida: o pulso é órfão.
# Um livelock REAL (Mac acordado há >15min, loop com >15min, pulso >15min) segue KILL.
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
VG_WAKETIME="${VG_WAKETIME:-}"
# VG_GRACA_SONO_S / VG_GRACA_NASCENTE_S: sem default aqui de propósito — vigia_decidir
# resolve para o limiar EM USO (VG_STALL_S) quando não setadas.

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

# ------------------------------------------------- o Mac acabou de ACORDAR? -----
# `sysctl -n kern.waketime` => "{ sec = 1789071287, usec = 206221 } Thu Sep 10 ...".
# Ecoa o epoch (s) ou vazio se ilegível. (O 1º "sec = " é o do campo sec; "usec" vem depois.)
_parse_waketime() {
    local s="$1"
    case "$s" in
        *"sec = "*) s="${s#*sec = }"; s="${s%%,*}" ;;
        *) echo ""; return ;;
    esac
    case "$s" in ''|*[!0-9]*) echo "" ;; *) echo "$s" ;; esac
}

# Segundos desde o último WAKE do Mac, ou -1 se desconhecido (sem sysctl, 0, futuro, lixo).
# VG_WAKETIME sobrepõe (teste). Leitura pura: não toca nada.
desde_acordar_s() {
    local w agora
    if [ -n "$VG_WAKETIME" ]; then w="$VG_WAKETIME"
    else w="$(_parse_waketime "$(sysctl -n kern.waketime 2>/dev/null)")"; fi
    case "$w" in ''|*[!0-9]*) echo -1; return ;; esac
    [ "$w" -le 0 ] && { echo -1; return; }
    agora="$(date +%s)"
    [ "$w" -gt "$agora" ] && { echo -1; return; }
    echo $(( agora - w ))
}

# ------------------------------------------------ idade do processo de LOOP -----
# `ps -o etime=` => "[[dd-]hh:]mm:ss" -> segundos; -1 se ilegível. 10# evita octal ("08").
_etime_para_s() {
    local e d=0 h=0 m=0 s=0 v
    e="$(printf '%s' "$1" | tr -d '[:space:]')"
    case "$e" in *-*) d="${e%%-*}"; e="${e#*-}" ;; esac
    local IFS=:
    # shellcheck disable=SC2086
    set -- $e
    case $# in
        2) m="$1"; s="$2" ;;
        3) h="$1"; m="$2"; s="$3" ;;
        *) echo -1; return ;;
    esac
    for v in "$d" "$h" "$m" "$s"; do
        case "$v" in ''|*[!0-9]*) echo -1; return ;; esac
    done
    echo $(( 10#$d*86400 + 10#$h*3600 + 10#$m*60 + 10#$s ))
}

# Idade (s) do processo de loop MAIS NOVO (a encarnação que o launchd acabou de subir),
# ou -1 se nenhum achado. Mais novo = conservador: se QUALQUER loop é recém-nascido, o
# pulso velho pode ser órfão da encarnação anterior.
loop_mais_novo() {  # ecoa "<idade_s> <pid>" do loop mais novo, ou "-1 -"
    local pid s menor=-1 pid_menor="-"
    for pid in $(_pids_do_loop); do
        s="$(_etime_para_s "$(ps -o etime= -p "$pid" 2>/dev/null)")"
        [ "$s" -lt 0 ] && continue
        if [ "$menor" -lt 0 ] || [ "$s" -lt "$menor" ]; then menor="$s"; pid_menor="$pid"; fi
    done
    echo "$menor $pid_menor"
}

idade_loop_s() {
    local x; x="$(loop_mais_novo)"
    echo "${x%% *}"
}

# Contexto do pulso p/ o LOG ("fase=passada curso=... ciclo=7 pid=123"): diz ONDE o loop
# parou. Best-effort (vazio se ilegível / pulso antigo sem os campos).
pulso_contexto() {
    [ -f "$VG_PULSO" ] || { echo ""; return; }
    python3 - "$VG_PULSO" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    print(""); raise SystemExit
print(" ".join("%s=%s" % (k, d[k]) for k in ("fase", "curso", "ciclo", "pid")
               if d.get(k) is not None))
PY
}

# ============================================================ DECISÃO (PURA) ===
# Sem I/O. Lê 3 fatos do ambiente e ecoa EXATAMENTE UM token de ação. Testável em
# tabela, impossível de mentir.
#   VG_DAEMON_LOADED : 1|0
#   VG_PULSO_AGE     : segundos (>=0) ou -1 (ausente)
#   VG_STALL_S       : limiar
#   VG_DESDE_ACORDAR : s desde o último wake do Mac  (-1/ausente = desconhecido)
#   VG_LOOP_IDADE    : s de vida do loop mais novo   (-1/ausente = desconhecido)
# Tokens:
#   KILL         -> carregado + pulso VELHO + Mac acordado há > graça + loop com > graça
#                   (livelock provado)
#   HEALTHY      -> carregado + pulso presente + fresco
#   SONO         -> pulso velho, mas o Mac ACORDOU há <= graça: loop estava congelado
#                   pelo SO, não travado. NÃO mata.
#   NASCENTE     -> pulso velho, mas o loop atual nasceu há <= graça: o pulso é da
#                   encarnação ANTERIOR. NÃO mata.
#   CRASHLOOP    -> NASCENTE de novo, mas o loop mais novo é OUTRO pid que o do tick
#                   anterior e o pulso segue velho: o daemon reinicia sem nunca pulsar.
#                   NÃO mata (não ajuda) — ESCALA (a graça não pode calar esse modo).
#   (VG_ESTADO_ANTERIOR / VG_PID_ANTERIOR / VG_LOOP_PID alimentam só o CRASHLOOP.)
#   DOWN_ALERT   -> NÃO carregado (dono desligou; só alerta, nunca ressuscita)
#   NO_HEARTBEAT -> carregado + pulso AUSENTE (sem prova; não mata)
# Fatos desconhecidos (-1) => exatamente a decisão ANTIGA (retrocompat).
vigia_decidir() {
    local loaded="${VG_DAEMON_LOADED:?}" age="${VG_PULSO_AGE:?}" stall="${VG_STALL_S:-900}"
    local acordou="${VG_DESDE_ACORDAR:--1}" loop_idade="${VG_LOOP_IDADE:--1}"
    local g_sono="${VG_GRACA_SONO_S:-$stall}" g_nasc="${VG_GRACA_NASCENTE_S:-$stall}"
    if [ "$loaded" != "1" ]; then echo "DOWN_ALERT"; return; fi
    if [ "$age" -lt 0 ]; then echo "NO_HEARTBEAT"; return; fi
    if [ "$age" -le "$stall" ]; then echo "HEALTHY"; return; fi
    if [ "$acordou" -ge 0 ] && [ "$acordou" -le "$g_sono" ]; then echo "SONO"; return; fi
    if [ "$loop_idade" -ge 0 ] && [ "$loop_idade" -le "$g_nasc" ]; then
        local ant="${VG_ESTADO_ANTERIOR:-}" pid_ant="${VG_PID_ANTERIOR:-}"
        local pid_ag="${VG_LOOP_PID:-}"
        case "$ant" in
            NASCENTE|CRASHLOOP)
                if [ -n "$pid_ant" ] && [ -n "$pid_ag" ] && [ "$pid_ant" != "-" ] \
                        && [ "$pid_ag" != "-" ] && [ "$pid_ant" != "$pid_ag" ]; then
                    echo "CRASHLOOP"; return
                fi ;;
        esac
        echo "NASCENTE"; return
    fi
    echo "KILL"
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
    local loaded age decisao acordou loop_idade loop_pid lm anterior="" pid_anterior=""
    loaded="$(daemon_carregado)"
    age="$(pulso_idade_s)"
    acordou="$(desde_acordar_s)"
    lm="$(loop_mais_novo)"; loop_idade="${lm%% *}"; loop_pid="${lm#* }"
    [ -f "$VG_STATE" ] && anterior="$(cat "$VG_STATE" 2>/dev/null)"
    [ -f "$VG_STATE.pid" ] && pid_anterior="$(cat "$VG_STATE.pid" 2>/dev/null)"

    decisao="$(VG_DAEMON_LOADED="$loaded" VG_PULSO_AGE="$age" VG_STALL_S="$VG_STALL_S" \
        VG_DESDE_ACORDAR="$acordou" VG_LOOP_IDADE="$loop_idade" VG_LOOP_PID="$loop_pid" \
        VG_ESTADO_ANTERIOR="$anterior" VG_PID_ANTERIOR="$pid_anterior" vigia_decidir)"

    case "$decisao" in
        HEALTHY)
            _log "OK — daemon carregado, pulso fresco (${age}s <= ${VG_STALL_S}s). Quieto."
            ;;
        SONO)
            _log "SONO — pulso velho (${age}s) mas o Mac ACORDOU há ${acordou}s: o loop estava CONGELADO pelo SO (Mac dormindo), não travado. NÃO mato; espero ele pulsar. Último pulso: [$(pulso_contexto)]"
            ;;
        NASCENTE)
            _log "NASCENTE — pulso velho (${age}s) mas o loop atual (PID ${loop_pid}) tem só ${loop_idade}s de vida: o pulso é ÓRFÃO da encarnação anterior. NÃO mato; espero o 1º pulso dele. Último pulso: [$(pulso_contexto)]"
            ;;
        CRASHLOOP)
            _log "CRASH-LOOP? — pulso velho (${age}s) e o loop recém-nascido (${loop_idade}s, PID ${loop_pid}) é OUTRO processo que o do tick anterior (PID ${pid_anterior}): o daemon reinicia sem nunca pulsar. NÃO mato (não ajuda); escalo. Veja ~/.athena-local/loop.err."
            if [ "$VG_DRY_RUN" != "1" ] && _deve_alertar CRASHLOOP; then
                telegram_alerta "Athena vigia-externo: daemon ${VG_DAEMON_LABEL} parece em CRASH-LOOP — reinicia (PID ${pid_anterior} -> ${loop_pid}) sem nunca gravar o pulso (${age}s). Não mato; veja ~/.athena-local/loop.err."
            fi
            ;;
        KILL)
            _log "LIVELOCK — daemon CARREGADO mas pulso VELHO (${age}s > ${VG_STALL_S}s; Mac acordado há ${acordou}s; loop com ${loop_idade}s de vida). Bounce do loop. Parou em: [$(pulso_contexto)]"
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
    # pid do loop mais novo neste tick: base da detecção de CRASHLOOP no próximo tick
    printf '%s' "$loop_pid" > "$VG_STATE.pid" 2>/dev/null
    echo "$decisao"
}

# Só executa main quando RODADO (não quando SOURCED pelos testes).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
