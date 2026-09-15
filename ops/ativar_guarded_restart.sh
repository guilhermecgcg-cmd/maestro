#!/bin/bash
# GUARDED RESTART do daemon com.athena.local (ATIVAR 22/07).
# Espera a janela idle REAL (0 motores + pulso ativos=[] fresco + locks vazio), e SÓ então
# faz unload -> confirma GONE -> load, UMA vez. Se a janela não aparecer no budget, NADA é
# feito (exit 2). ANTI-BAN: nunca mata um motor em voo.
set -u
PLIST="/Users/guilhermerodrigues/Library/LaunchAgents/com.athena.local.plist"
PULSO="/Users/guilhermerodrigues/.athena-local/pulso.json"
LOCKS="/Users/guilhermerodrigues/.athena-local/locks"
LABEL="com.athena.local"
MAX_ITERS=90          # 90 x 10s = 15 min de budget
SLEEP_S=10
FRESH_MAX=95          # pulso precisa ter <95s de idade (daemon vivo + runway no sleep de 120s)
RUNWAY_MIN_AGE=0      # aceita qualquer pulso fresco; runway = 120 - idade

log(){ echo "[$(date '+%H:%M:%S')] $*"; }

motores_vivos(){
  # conta subprocessos de captura reais (motor.cli hotmart + reseed multiplataforma)
  pgrep -f 'python -m motor\.cli' 2>/dev/null | wc -l | tr -d ' '
}
reseed_vivos(){ pgrep -f 'reseed_plataforma' 2>/dev/null | grep -v $$ | wc -l | tr -d ' '; }
locks_count(){ ls -1 "$LOCKS" 2>/dev/null | wc -l | tr -d ' '; }
pulso_ativos_n(){ python3 -c "import json;d=json.load(open('$PULSO'));print(len(d.get('ativos',[])))" 2>/dev/null || echo 999; }
pulso_ciclo(){ python3 -c "import json;print(json.load(open('$PULSO'))['ciclo'])" 2>/dev/null || echo -1; }
pulso_age(){ python3 -c "import json,time;print(int(time.time()-json.load(open('$PULSO'))['ts']))" 2>/dev/null || echo 99999; }
daemon_pid(){ launchctl list 2>/dev/null | awk -v l="$LABEL" '$3==l{print $1}'; }

log "INICIO guarded restart. daemon_pid=$(daemon_pid) ciclo=$(pulso_ciclo) ativos_n=$(pulso_ativos_n) locks=$(locks_count) motores=$(motores_vivos)"

i=0
while [ $i -lt $MAX_ITERS ]; do
  i=$((i+1))
  m=$(motores_vivos); r=$(reseed_vivos); lk=$(locks_count); an=$(pulso_ativos_n); ag=$(pulso_age); cy=$(pulso_ciclo)
  if [ "$m" = "0" ] && [ "$r" = "0" ] && [ "$lk" = "0" ] && [ "$an" = "0" ] && [ "$ag" -lt "$FRESH_MAX" ]; then
    log "JANELA IDLE detectada (iter $i): motores=0 reseed=0 locks=0 ativos=0 pulso_age=${ag}s ciclo=$cy -> runway ~$((120-ag))s"
    break
  fi
  log "aguardando (iter $i/$MAX_ITERS): motores=$m reseed=$r locks=$lk ativos=$an pulso_age=${ag}s ciclo=$cy"
  sleep $SLEEP_S
done

# re-checa NA HORA (evita agir sobre leitura velha)
m=$(motores_vivos); r=$(reseed_vivos); lk=$(locks_count); an=$(pulso_ativos_n); ag=$(pulso_age)
if ! { [ "$m" = "0" ] && [ "$r" = "0" ] && [ "$lk" = "0" ] && [ "$an" = "0" ] && [ "$ag" -lt "$FRESH_MAX" ]; }; then
  log "SEM JANELA no budget (motores=$m reseed=$r locks=$lk ativos=$an age=${ag}s). NADA FEITO. Daemon segue vivo, flags NÃO ativadas ainda."
  echo "RESULTADO=HELD"
  exit 2
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

log "LOAD (subindo daemon com flags ATHENA_NAO_VIDEO_ATIVO=1 + ATHENA_SISTEMAS_PATH wired)"
launchctl load "$PLIST"
sleep 3
NPID=$(daemon_pid)
log "daemon carregado: pid=$NPID"
# espera um pulso novo (ciclo avanca / ts atualiza)
ciclo0=$(pulso_ciclo); ts_ok=0
for k in $(seq 1 30); do
  ag=$(pulso_age); cy=$(pulso_ciclo)
  if [ "$ag" -lt 30 ]; then ts_ok=1; log "PULSO NOVO: ciclo=$cy age=${ag}s"; break; fi
  sleep 2
done
echo "RESULTADO=ATIVADO PID=$NPID CICLO=$(pulso_ciclo) PULSO_FRESH=$ts_ok"
exit 0
