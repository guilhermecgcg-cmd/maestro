"""REFINO 2 — DENTES nas partes PURAS do preencher_totais: o parsing dos cursos e,
sobretudo, o REWRITE do YAML (a operação que, se errada, corromperia o course-list do
daemon). Nada de Playwright/rede aqui — só as funções puras.

Por que dentes no rewrite: o arquivo tem comentários carregados de sentido (nome do
curso, tracker_lessons). Um rewrite que os apagasse, ou que mexesse num curso que a
parede impediu de ler, quebraria o inviolável 'não invento total' / fail-closed."""
import importlib.util
import os

import pytest

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "preencher_totais.py")
_spec = importlib.util.spec_from_file_location("preencher_totais", _SCRIPT)
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)


YAML = '''\
- url: "https://hotmart.com/pt-br/club/x/products/111"  # tracker_lessons=1 (parcial)
  conta: "hotmart-principal"
  plataforma: hotmart
  total_esperado: 0        # "Curso Um"

- url: "https://hotmart.com/pt-br/club/y/products/222"  # sem tracker
  conta: "hotmart-principal"
  plataforma: hotmart
  total_esperado: 0        # "Curso Dois"

- url: "https://minha.memberkit.com.br/333"
  conta: "mk"
  plataforma: memberkit
  total_esperado: 0        # "Curso MK"
'''


def test_parse_cursos_ordem_e_plataforma():
    cursos = pt.parse_cursos(YAML)
    assert [c["url"] for c in cursos] == [
        "https://hotmart.com/pt-br/club/x/products/111",
        "https://hotmart.com/pt-br/club/y/products/222",
        "https://minha.memberkit.com.br/333"]
    # DENTES: a plataforma memberkit NÃO pode ser lida como hotmart (o loop só busca
    # navegação de cursos hotmart; ler mk como hotmart tentaria /v1/navigation nele).
    assert cursos[2]["plataforma"] == "memberkit"
    assert cursos[0]["plataforma"] == "hotmart"


def test_parse_cursos_sem_plataforma_so_e_hotmart_no_host_do_hotmart():
    # ESPELHO do carregar_cursos: um bloco SEM `plataforma:` só é Hotmart num host
    # hotmart.com — este script nunca pede /v1/navigation com a sessão do Hotmart num
    # tenant de domínio próprio (ex.: um Cademí posto no YAML sem a linha).
    texto = ('- url: "https://aulas.novotenant.com.br/"\n  conta: "c"\n'
             '- url: "https://evilhotmart.com/x/products/1"\n  conta: "e"\n'
             '- url: "https://sub.hotmart.com/x/products/2"\n  conta: "h"\n'
             '- url: "https://hotmart.com/pt-br/club/z/products/3"\n  conta: "h"\n')
    assert [c["plataforma"] for c in pt.parse_cursos(texto)] == [
        "nao-declarada", "nao-declarada", "hotmart", "hotmart"]


def test_rewrite_troca_so_o_numero_e_preserva_comentario():
    out = pt.atualizar_yaml_texto(
        YAML, {"https://hotmart.com/pt-br/club/x/products/111": 42})
    # o total virou 42 E o comentário/nome do curso sobreviveu intacto.
    assert 'total_esperado: 42        # "Curso Um"' in out
    # DENTES: o comentário do 111 (nome + tracker) não pode ter sumido.
    assert '# "Curso Um"' in out
    assert "tracker_lessons=1" in out


def test_rewrite_NAO_toca_curso_nao_lido_fica_em_zero():
    # Só o 111 foi lido; o 222 (parede antes dele) e o mk NÃO estão em `totais`.
    out = pt.atualizar_yaml_texto(
        YAML, {"https://hotmart.com/pt-br/club/x/products/111": 42})
    linhas = [l for l in out.splitlines() if "total_esperado" in l]
    # DENTES: o 111 mudou p/ 42; os outros DOIS seguem em 0 (fail-closed, não inventado).
    assert 'total_esperado: 42' in linhas[0]
    assert linhas[1].strip().startswith("total_esperado: 0")
    assert linhas[2].strip().startswith("total_esperado: 0")


def test_rewrite_idempotente():
    totais = {"https://hotmart.com/pt-br/club/x/products/111": 42,
              "https://hotmart.com/pt-br/club/y/products/222": 7}
    uma = pt.atualizar_yaml_texto(YAML, totais)
    duas = pt.atualizar_yaml_texto(uma, totais)
    assert uma == duas
    assert 'total_esperado: 42        # "Curso Um"' in uma
    assert 'total_esperado: 7        # "Curso Dois"' in uma


def test_rewrite_conta_o_bloco_certo_urls_parecidas():
    # anti-troca-de-bloco: uma URL que é PREFIXO de outra (111 vs 1110) não pode fazer o
    # rewrite escrever no total errado. Cada `- url:` reinicia o bloco corrente.
    y = ('- url: "https://h.com/products/111"\n'
         '  total_esperado: 0        # A\n'
         '- url: "https://h.com/products/1110"\n'
         '  total_esperado: 0        # B\n')
    out = pt.atualizar_yaml_texto(y, {"https://h.com/products/1110": 99})
    linhas = [l for l in out.splitlines() if "total_esperado" in l]
    assert linhas[0].strip().startswith("total_esperado: 0")   # 111 intacto
    assert "total_esperado: 99" in linhas[1]                   # só o 1110 mudou
