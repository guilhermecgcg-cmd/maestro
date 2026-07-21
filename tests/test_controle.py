"""maestro.controle — o interruptor MECÂNICO de re-disparo por plataforma/conta.

O que se prova aqui (disciplina dos dublês: nada de rede, FS real em tmp_path):
  - filtrar_cursos LÊ o controle.yaml e REMOVE da lista os cursos cuja plataforma
    (ou conta) está pausada. É o gatilho que o ciclo doméstico chama a cada 120s:
    pausar = o curso some da lista disparável -> o ciclo não RE-dispara. NÃO mata
    nada (Ordem IV): só corta o re-disparo.
  - a gravação é ATÔMICA (os.replace): um leitor concorrente nunca vê meio-arquivo,
    e um crash no meio não corrompe o controle.
  - fail-open p/ ATIVO quando o arquivo NÃO existe (1º boot: nada pausado); fail-LOUD
    (levanta) quando o arquivo existe mas está corrompido (nunca pausar/despausar
    silenciosamente por um YAML ilegível — o ciclo escala honesto).
  - capturas_vivas LÊ os lockfiles do LocalExecutor e devolve só as com PID VIVO
    (o mesmo mecanismo-verdade do anti-ban; injeta-se a sonda de PID nos testes).
"""
import json
import os
from dataclasses import dataclass

import pytest

from maestro import controle


@dataclass
class _Curso:
    """Dublê enxuto de CursoLocal — só os campos que o filtro lê."""
    url: str
    conta: str
    plataforma: str = "hotmart"


def _cursos():
    return [
        _Curso("http://h/1", "hotmart-principal", "hotmart"),
        _Curso("http://s/1", "stoa-conta", "stoa"),
        _Curso("http://s/2", "stoa-conta", "stoa"),
        _Curso("http://k/1", "kajabi-conta", "kajabi"),
    ]


# --- filtrar_cursos ---------------------------------------------------------
def test_arquivo_ausente_nada_pausado_devolve_todos(tmp_path):
    # 1º boot: sem controle.yaml -> NADA pausado -> a lista passa inteira.
    path = str(tmp_path / "controle.yaml")
    assert controle.filtrar_cursos(_cursos(), path) == _cursos()


def test_pausar_plataforma_remove_os_cursos_dela(tmp_path):
    path = str(tmp_path / "controle.yaml")
    controle.pausar_plataforma(path, "stoa")
    restantes = controle.filtrar_cursos(_cursos(), path)
    plats = {c.plataforma for c in restantes}
    assert "stoa" not in plats                 # os 2 cursos de stoa sumiram
    assert {"hotmart", "kajabi"} == plats
    assert len(restantes) == 2


def test_pausar_conta_remove_os_cursos_daquela_conta(tmp_path):
    path = str(tmp_path / "controle.yaml")
    controle.pausar_conta(path, "stoa-conta")
    restantes = controle.filtrar_cursos(_cursos(), path)
    assert all(c.conta != "stoa-conta" for c in restantes)
    assert len(restantes) == 2                 # sobra hotmart + kajabi


def test_ativar_plataforma_reverte_o_pause(tmp_path):
    path = str(tmp_path / "controle.yaml")
    controle.pausar_plataforma(path, "stoa")
    assert len(controle.filtrar_cursos(_cursos(), path)) == 2
    controle.ativar_plataforma(path, "stoa")
    # DENTE: reativar tem que fazer os cursos de stoa VOLTAREM a ser disparáveis.
    assert controle.filtrar_cursos(_cursos(), path) == _cursos()


def test_pausar_e_case_insensitive_na_plataforma(tmp_path):
    # 'pausar STOA' pausa 'stoa' — a plataforma casa sem depender de caixa.
    path = str(tmp_path / "controle.yaml")
    controle.pausar_plataforma(path, "STOA")
    assert all(c.plataforma != "stoa" for c in controle.filtrar_cursos(_cursos(), path))


def test_pausar_e_idempotente_nao_duplica(tmp_path):
    path = str(tmp_path / "controle.yaml")
    controle.pausar_plataforma(path, "stoa")
    est = controle.pausar_plataforma(path, "stoa")     # 2ª vez
    assert est["plataformas_pausadas"].count("stoa") == 1


# --- gravação ATÔMICA -------------------------------------------------------
def test_gravacao_e_atomica_via_os_replace(tmp_path, monkeypatch):
    # DENTE: a troca final TEM que ser os.replace (rename atômico), nunca um write
    # direto no destino (que exporia meio-arquivo a um leitor concorrente / a um crash).
    path = str(tmp_path / "controle.yaml")
    chamadas = {"replace": 0, "destino": []}
    real_replace = os.replace

    def spy(src, dst):
        chamadas["replace"] += 1
        chamadas["destino"].append(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(controle.os, "replace", spy)
    controle.pausar_plataforma(path, "stoa")
    assert chamadas["replace"] == 1
    assert chamadas["destino"] == [path]               # o rename cai no destino final
    # e não deixa .tmp órfão para trás
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []


def test_arquivo_gravado_e_yaml_valido_relegivel(tmp_path):
    path = str(tmp_path / "controle.yaml")
    controle.pausar_plataforma(path, "stoa")
    controle.pausar_conta(path, "conta-x")
    est = controle.carregar(path)                      # relê do disco
    assert "stoa" in est["plataformas_pausadas"]
    assert "conta-x" in est["contas_pausadas"]


# --- fail-LOUD em corrupção (nunca pausar/despausar em silêncio) ------------
def test_controle_corrompido_levanta_nao_silencia(tmp_path):
    # DENTE: um controle.yaml ilegível NÃO pode virar "nada pausado" (ignoraria um
    # pause do dono) NEM "tudo pausado" (mataria o never-stop). Levanta -> o ciclo
    # escala honesto em vez de decidir às cegas.
    path = tmp_path / "controle.yaml"
    path.write_text("isto: [nao, fecha\n")             # YAML quebrado
    with pytest.raises(Exception):
        controle.filtrar_cursos(_cursos(), str(path))


def test_controle_nao_mapa_levanta(tmp_path):
    path = tmp_path / "controle.yaml"
    path.write_text("- uma\n- lista\n")                # YAML válido mas não é mapa
    with pytest.raises(ValueError):
        controle.carregar(str(path))


def test_arquivo_vazio_e_tratado_como_nada_pausado(tmp_path):
    path = tmp_path / "controle.yaml"
    path.write_text("")                                # vazio -> None no safe_load
    assert controle.filtrar_cursos(_cursos(), str(path)) == _cursos()


# --- capturas_vivas: verdade dos lockfiles (bate com ps) --------------------
def _escrever_lock(lock_dir, conta, curso_url, pid):
    import hashlib
    os.makedirs(lock_dir, exist_ok=True)
    slug = hashlib.sha256(str(conta).encode()).hexdigest()[:16]
    with open(os.path.join(lock_dir, slug + ".lock"), "w") as f:
        json.dump({"pid": pid, "course_url": curso_url, "conta": str(conta)}, f)


def test_capturas_vivas_so_conta_pid_vivo(tmp_path):
    lock_dir = str(tmp_path / "locks")
    _escrever_lock(lock_dir, "stoa-conta", "http://s/1", 111)     # vivo
    _escrever_lock(lock_dir, "kajabi-conta", "http://k/1", 222)   # morto
    vivos = {111}
    out = controle.capturas_vivas(lock_dir, pid_vivo=lambda p: p in vivos)
    assert len(out) == 1
    assert out[0]["course_url"] == "http://s/1"
    assert out[0]["conta"] == "stoa-conta"


def test_capturas_vivas_dir_ausente_e_vazio(tmp_path):
    # sem nenhum lock (dir não existe) -> zero capturas vivas, sem levantar.
    assert controle.capturas_vivas(str(tmp_path / "nada"), pid_vivo=lambda p: True) == []


def test_capturas_vivas_ignora_lock_de_intencao_sem_pid(tmp_path):
    # lock de INTENÇÃO (pid=None, gravado antes do spawn): não sabemos o PID a sondar,
    # mas a conta está OCUPADA -> conta como viva (fail-closed anti-ban), não some.
    lock_dir = str(tmp_path / "locks")
    _escrever_lock(lock_dir, "stoa-conta", "http://s/1", None)
    out = controle.capturas_vivas(lock_dir, pid_vivo=lambda p: False)
    assert len(out) == 1 and out[0]["course_url"] == "http://s/1"
