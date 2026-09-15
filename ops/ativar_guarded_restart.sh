#!/bin/bash
# GUARDED RESTART do daemon com.athena.local (ATIVAR 22/07; no repositório desde 15/09 — D3, item 8).
# Espera a janela idle REAL (0 motores + 0 reseed + nenhum lock que segure + pulso ativos=[] fresco),
# e SÓ então faz unload -> confirma GONE -> load, UMA vez. Se a janela não aparecer no budget, NADA é
# feito (exit 2). ANTI-BAN: nunca mata um motor em voo.
#
# D3, item 8 (15/09):
#   - MOTOR VIVO = `[Pp]ython…` + opções do interpretador + `-m motor.` (ver MOTOR abaixo). Antes só
#     `python -m motor\.cli`: motor.entregadigital, motor.kiwify, motor.instagram (e o
#     `python3.14 -m ...` do venv) eram invisíveis e o restart podia acontecer com captura em
#     andamento. D3r2, A3: o `python[^ ]* -m motor\.` do D3 ainda não via `python -u -m motor.cli`,
#     `-X dev`/`-W ação` antes do `-m`, nem o `Python` de framework do macOS (argv[0] maiúsculo).
#   - LOCK: só NÃO segura a janela o de dono explicitamente MANUAL (sonda-p102, motor-anexos,
#     motor-retentar, manual...) cujo PID não descende do daemon — a prova manual não é afetada pelo
#     restart e segurava a janela para sempre. O motor desse lock (e o que descende dele) também não
#     segura. Lock sem dono, ilegível, de PID ausente ou filho do daemon: segura (conservador).
#   - Costuras de teste por env (AGR_*), com os caminhos de produção como padrão. AGR_SO_GUARDA=1
#     decide a janela e sai ANTES de qualquer unload/load.
set -u
PLIST="${AGR_PLIST:-/Users/guilhermerodrigues/Library/LaunchAgents/com.athena.local.plist}"
PULSO="${AGR_PULSO:-/Users/guilhermerodrigues/.athena-local/pulso.json}"
LOCKS="${AGR_LOCKS:-/Users/guilhermerodrigues/.athena-local/locks}"
LABEL="${AGR_LABEL:-com.athena.local}"
MAX_ITERS="${AGR_MAX_ITERS:-90}"      # 90 x 10s = 15 min de budget
SLEEP_S="${AGR_SLEEP_S:-10}"
FRESH_MAX=95          # pulso precisa ter <95s de idade (daemon vivo + runway no sleep de 120s)

log(){ echo "[$(date '+%H:%M:%S')] $*"; }

daemon_pid(){ launchctl list 2>/dev/null | awk -v l="$LABEL" '$3==l{print $1}'; }

# "<motores que seguram> <locks que seguram>" — lê `ps` e os locks num python só de biblioteca
# padrão (o python3 do sistema, como as leituras de pulso abaixo).
bloqueios(){
  python3 - "$LOCKS" "$(daemon_pid)" <<'PY'
import json, os, re, subprocess, sys

locks_dir, daemon = sys.argv[1], sys.argv[2].strip()
MANUAIS = ("sonda-p102", "motor-anexos", "motor-retentar")
tabela = {}
try:
    saida = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True,
                           text=True).stdout
except Exception:
    saida = ""
for linha in saida.splitlines():
    partes = linha.strip().split(None, 2)
    if len(partes) >= 2 and partes[0].isdigit() and partes[1].isdigit():
        tabela[int(partes[0])] = (int(partes[1]), partes[2] if len(partes) > 2 else "")
if not tabela:
    print("999 999")        # ps ilegível: fail-closed, a janela não abre
    sys.exit(0)
raiz = int(daemon) if daemon.isdigit() else None


def descende_de(pid, alvos):
    vistos = set()
    while pid in tabela and pid not in vistos:
        if pid in alvos:
            return True
        vistos.add(pid)
        pid = tabela[pid][0]
    return pid in alvos


aceitos, locks_que_seguram = set(), 0
nomes = sorted(os.listdir(locks_dir)) if os.path.isdir(locks_dir) else []
for nome in nomes:
    dados = None
    try:
        with open(os.path.join(locks_dir, nome)) as f:
            dados = json.load(f)
    except Exception:
        pass
    dono = str(dados.get("dono") or "") if isinstance(dados, dict) else ""
    pid = dados.get("pid") if isinstance(dados, dict) else None
    manual = dono in MANUAIS or dono.startswith("manual")
    if (nome.endswith(".lock") and manual and isinstance(pid, int) and not isinstance(pid, bool)
            and pid > 0 and not (raiz is not None and descende_de(pid, {raiz}))):
        aceitos.add(pid)
        continue
    locks_que_seguram += 1

# D3r2, A3: opções do interpretador antes do `-m` (`-u`, `-B`, `-X dev`, `-W ação`, `-mmotor.x`) e o
# `Python` de framework do macOS também são motor. As alternativas não se sobrepõem (sem backtracking
# explosivo numa linha com muitas opções). Errar para o lado de segurar só adia o restart.
MOTOR = re.compile(r"[Pp]ython[^ /]*(?: +-[XW] +[^ ]+| +-[A-Za-z]+)* +-m *motor\.")
motores = [p for p, (_ppid, args) in tabela.items()
           if MOTOR.search(args) and not descende_de(p, aceitos)]
print(len(motores), locks_que_seguram)
PY
}
motores_vivos(){ bloqueios | awk '{print $1}'; }
reseed_vivos(){ pgrep -f 'reseed_plataforma' 2>/dev/null | grep -v $$ | wc -l | tr -d ' '; }
pulso_ativos_n(){ python3 -c "import json;d=json.load(open('$PULSO'));print(len(d.get('ativos',[])))" 2>/dev/null || echo 999; }
pulso_ciclo(){ python3 -c "import json;print(json.load(open('$PULSO'))['ciclo'])" 2>/dev/null || echo -1; }
pulso_age(){ python3 -c "import json,time;print(int(time.time()-json.load(open('$PULSO'))['ts']))" 2>/dev/null || echo 99999; }

janela(){  # imprime "m r lk an ag" da leitura atual
  local m lk
  read -r m lk <<< "$(bloqueios)"
  echo "$m $(reseed_vivos) $lk $(pulso_ativos_n) $(pulso_age)"
}

log "INICIO guarded restart. daemon_pid=$(daemon_pid) ciclo=$(pulso_ciclo) ativos_n=$(pulso_ativos_n)"

i=0
while [ "$i" -lt "$MAX_ITERS" ]; do
  i=$((i+1))
  read -r m r lk an ag <<< "$(janela)"; cy=$(pulso_ciclo)
  if [ "$m" = "0" ] && [ "$r" = "0" ] && [ "$lk" = "0" ] && [ "$an" = "0" ] && [ "$ag" -lt "$FRESH_MAX" ]; then
    log "JANELA IDLE detectada (iter $i): motores=0 reseed=0 locks_que_seguram=0 ativos=0 pulso_age=${ag}s ciclo=$cy -> runway ~$((120-ag))s"
    break
  fi
  log "aguardando (iter $i/$MAX_ITERS): motores=$m reseed=$r locks_que_seguram=$lk ativos=$an pulso_age=${ag}s ciclo=$cy"
  sleep "$SLEEP_S"
done

# re-checa NA HORA (evita agir sobre leitura velha)
read -r m r lk an ag <<< "$(janela)"
if ! { [ "$m" = "0" ] && [ "$r" = "0" ] && [ "$lk" = "0" ] && [ "$an" = "0" ] && [ "$ag" -lt "$FRESH_MAX" ]; }; then
  log "SEM JANELA no budget (motores=$m reseed=$r locks_que_seguram=$lk ativos=$an age=${ag}s). NADA FEITO. Daemon segue vivo."
  echo "RESULTADO=HELD"
  exit 2
fi
if [ "${AGR_SO_GUARDA:-0}" = "1" ]; then
  log "SÓ GUARDA (AGR_SO_GUARDA=1): a janela abriu; nenhum unload/load feito."
  echo "RESULTADO=GUARDA_OK"
  exit 0
fi

PID_ANTES=$(daemon_pid)
log "GUARD OK -> UNLOAD (pid antes=$PID_ANTES)"
launchctl unload "$PLIST"
# confirma GONE
gone=0
for k in $(seq 1 20); do
  p=$(daemon_pid)
  still=$(pgrep -f 'maestro.athena_local' 2>/dev/null | wc -l | tr -d ' ')
  if [ -z "$p" ] && [ "$still" = "0" ]; then gone=1; log "daemon GONE (unload confirmado, iter $k)"; break; fi
  sleep 1
done
if [ "$gone" != "1" ]; then
  log "FALHA: daemon NAO saiu apos unload (pid=$(daemon_pid)). ABORTANDO antes do load para nao duplicar."
  echo "RESULTADO=ERRO_UNLOAD"
  exit 3
fi
# guarda extra: nenhum motor orfao
mo=$(motores_vivos)
if [ "$mo" != "0" ]; then
  log "ATENCAO: $mo motor(es) apos unload (orfao?). Mesmo assim daemon esta fora; NAO carrego ate zerar."
  for k in $(seq 1 30); do mo=$(motores_vivos); [ "$mo" = "0" ] && break; sleep 2; done
  log "motores agora=$mo"
fi

log "LOAD (subindo o daemon com o código e o launch.sh atuais)"
launchctl load "$PLIST"
sleep 3
NPID=$(daemon_pid)
log "daemon carregado: pid=$NPID"
# espera um pulso novo (ciclo avanca / ts atualiza)
ts_ok=0
for k in $(seq 1 30); do
  ag=$(pulso_age); cy=$(pulso_ciclo)
  if [ "$ag" -lt 30 ]; then ts_ok=1; log "PULSO NOVO: ciclo=$cy age=${ag}s"; break; fi
  sleep 2
done
echo "RESULTADO=ATIVADO PID=$NPID CICLO=$(pulso_ciclo) PULSO_FRESH=$ts_ok"
exit 0
