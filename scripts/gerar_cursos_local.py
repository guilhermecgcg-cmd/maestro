#!/usr/bin/env python3
"""Gera o YAML de course-list DOMÉSTICO (ATHENA_LOCAL_CURSOS) a partir da lista de
compras Hotmart já raspada (cursos_hotmart_club.json) + o tracker local (tracker.db).

Emite, por curso: {url, conta, plataforma, total_esperado}, o contrato que
`maestro.athena_local.carregar_cursos` consome.

DECISÕES (o PORQUÊ, para não se refazer depois):

  conta = UM único valor para TODOS os cursos Hotmart. O anti-ban inviolável é
  "1 captura por conta", onde `conta` é a SESSÃO/login do browser — e existe UMA só
  sessão Hotmart (.hotmart-session.json). Acessar clubs (slugs) diferentes usa a MESMA
  login: 2 capturas simultâneas de clubs diferentes = 2 browsers na MESMA conta = risco
  de ban. Logo todos compartilham a conta e o loop os serializa 1-a-1 (emergente do
  guard por-conta do LocalExecutor). Slugs diferentes NÃO são contas diferentes.

  total_esperado = 0 para TODOS (FAIL-CLOSED), de propósito, e NÃO o count do tracker.
  Por quê (contraria a intuição de "usar o count"): o tracker é uma ENUMERAÇÃO de
  capturas passadas, muitas PARCIAIS — 13 dos 27 cursos com tracker têm 1-3 aulas
  (ex.: "100 Ganchos Estratégicos" com 1), e 9 mostram exatamente 20 (cara de teto de
  paginação, não de total real). Usar um denominador BAIXO faria o gate de completude
  (`no_notion >= total`) disparar cedo e declarar o curso CONCLUÍDO com o curso ainda
  pela metade — o exato FALSO-PRONTO que o Inviolável 4 proíbe (abandonar um parcial
  como pronto). 0 = o owner NUNCA declara done (fail-closed): re-checa/retoma, jamais
  abandona. O count do tracker fica preservado como COMENTÁRIO (referência humana),
  não como total ativo. Denominador confiável por curso = pendência explícita (só uma
  re-enumeração ao vivo, ou totais fornecidos por humano, resolve; o único conhecido é
  Invisto Direito = 537, verificado à mão).

Uso:
  python scripts/gerar_cursos_local.py \
      --json ~/.claude/jobs/35490c8a/tmp/cursos_hotmart_club.json \
      --tracker ~/teste/aula/tracker.db \
      --conta hotmart-principal \
      --out ~/.athena-local/cursos_hotmart.yaml
"""
import argparse
import json
import os
import sqlite3


def _yaml_escape(s: str) -> str:
    # aspas duplas + escape de barra/aspas: nomes/URLs têm ™, acentos, :, espaços.
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def gerar(json_path, tracker_path, conta, plataforma="hotmart"):
    cursos = json.load(open(json_path))
    counts = {}
    if tracker_path and os.path.exists(tracker_path):
        db = sqlite3.connect(tracker_path)
        counts = dict(db.execute(
            "SELECT course_id, count(*) FROM lessons GROUP BY course_id"))
        db.close()
    linhas = [
        "# course-list DOMÉSTICO da Athena (ATHENA_LOCAL_CURSOS) — GERADO por",
        "# scripts/gerar_cursos_local.py. Editável à mão. total_esperado=0 = FAIL-CLOSED",
        "# (owner nunca declara 'concluído'): ver o cabeçalho do gerador para o porquê.",
        f"# conta única='{conta}' (1 sessão Hotmart -> 1-por-conta -> captura serial).",
        "",
    ]
    for c in sorted(cursos, key=lambda x: str(x.get("nome", ""))):
        n = int(counts.get(str(c["id"]), 0))
        ref = f"  # tracker_lessons={n} (NÃO é total: enumeração pode ser parcial)" if n else \
              "  # sem tracker"
        linhas.append(f"- url: {_yaml_escape(c['url'])}{ref}")
        linhas.append(f"  conta: {_yaml_escape(conta)}")
        linhas.append(f"  plataforma: {plataforma}")
        linhas.append(f"  total_esperado: 0        # {_yaml_escape(c.get('nome',''))[:60]}")
        linhas.append("")
    return "\n".join(linhas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--tracker", default="")
    ap.add_argument("--conta", default="hotmart-principal")
    ap.add_argument("--plataforma", default="hotmart")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    txt = gerar(os.path.expanduser(a.json), os.path.expanduser(a.tracker),
                a.conta, a.plataforma)
    out = os.path.expanduser(a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(txt)
    n = txt.count("\n- url:") + (1 if txt.lstrip().startswith("- url:") else 0)
    print(f"escrito: {out} ({txt.count('- url:')} cursos)")


if __name__ == "__main__":
    main()
