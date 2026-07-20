"""GUARD ANTI-DUPLICIDADE (dono: Athena) no protocolo de captura.

O usuario foi queimado por RE-capturar um curso ja pronto (Invisto Direito: 485
aulas ja no Notion, mas uma re-enumeracao sob um rotulo duplicado fez o sistema
achar que estava pendente). Diretiva: a garantia de NAO-duplicidade no INICIO da
captura e da Athena e tem de sobreviver ao Mac desligar / a sessao acabar — todo
ciclo (inclusive apos restart) a Athena re-checa a FONTE DA VERDADE (Notion) antes
de enfileirar, e NAO re-enfileira curso ja capturado.

Os dubles MODELAM O MECANISMO, nao a coreografia:

  - FakeNotionApp.exec_app REPRODUZ o container do app: faz o MESMO shlex-parse que
    o `sh -c` faria no argv, extrai o PREFIXO passado e aplica o MESMO `starts_with`
    que o filtro de URL do Notion aplica sobre a propriedade "Origem". Assim:
      * se o codigo esquecer de tirar a query string -> o prefixo carrega '?...'
        -> nenhuma aula casa -> (False, 0)  [teeth da normalizacao];
      * se o codigo esquecer o limite de fronteira '/' -> um curso de URL
        parecida-mas-distinta casa por engano  [teeth do prefixo].

  - DoubleChecar: callable seam (curso_url) -> (tem_aulas, qtd), o formato exato
    que `coordenar` injeta. Ja-no-Notion -> (True, N); novo -> (False, 0).
"""
import shlex

import pytest

from maestro.adaptadores import captura
from maestro.registro import Projeto


def _proj(**kw):
    base = dict(nome="captura", projeto_easypanel="conhecimentoinfinito",
                servicos=("worker",), adaptador="captura",
                db_container="conhecimentoinfinito_db", db_name="conhecimento",
                db_user="postgres",
                app_container="conhecimentoinfinito_conhecimentoinfinito",
                gerenciar=True)
    base.update(kw)
    return Projeto(**base)


AGORA = 1_000_000.0
CURSO = "https://hotmart.com/pt-br/marketplace/produtos/x/products/3486759"
# aulas reais desse curso: a URL da aula COMECA pela URL do curso + '/content/...'
AULAS = [CURSO + "/content/AAA", CURSO + "/content/BBB", CURSO + "/content/CCC"]


class FakeNotionApp:
    """Modela o container do app: exec_app re-parseia o argv como o `sh -c` faria e
    aplica o MESMO starts_with que o filtro de URL do Notion aplica sobre 'Origem'."""
    def __init__(self, lessons=(), boom=False, saida=None):
        self.lessons = list(lessons)
        self.boom = boom
        self.saida = saida            # forcar uma saida crua (sem a sentinela) p/ teeth honesto
        self.comandos = []

    def exec_app(self, container, comando, timeout=None):
        self.comandos.append((container, comando, timeout))
        if self.boom:
            raise RuntimeError("docker off")
        if self.saida is not None:
            return self.saida
        # o `sh -c` do container quebraria o argv assim; o ultimo token e o PREFIXO.
        prefixo = shlex.split(comando)[-1]
        n = sum(1 for u in self.lessons if u.startswith(prefixo))
        return f"{captura.NOTION_SENTINELA} {n}\n"


class FakeVoz:
    def __init__(self):
        self.avisos = []
        self.escaladas = []

    def avisar_acao(self, acao):
        self.avisos.append(acao)

    def escalar(self, problema, pedido):
        self.escaladas.append((problema, pedido))


class FakeExecutor:
    def __init__(self, confirmacao="enfileirado:res"):
        self._confirmacao = confirmacao
        self.disparos = []

    def disparar(self, curso_url):
        self.disparos.append(curso_url)
        return self._confirmacao


class DoubleChecar:
    """Seam injetado: (curso_url) -> (tem_aulas, qtd). Ja-no-Notion -> (True,N)."""
    def __init__(self, ja=(), quantidade=485):
        self._ja = set(ja)
        self._q = quantidade
        self.chamadas = []

    def __call__(self, curso_url):
        self.chamadas.append(curso_url)
        if curso_url in self._ja:
            return (True, self._q)
        return (False, 0)


# ==========================================================================
# curso_ja_no_notion — consulta o Notion por PREFIXO de URL da 'Origem'
# ==========================================================================
def test_curso_com_aulas_no_notion_retorna_true_e_quantidade():
    ac = FakeNotionApp(lessons=AULAS)
    tem, qtd = captura.curso_ja_no_notion(_proj(), ac, CURSO)
    assert tem is True and qtd == 3
    # alcancou o Notion DE DENTRO do container do app (reusa o token do app), nao
    # adicionou segredo novo ao Maestro.
    cont, comando, _ = ac.comandos[0]
    assert cont == "conhecimentoinfinito_conhecimentoinfinito"
    assert "notion_client" in comando and captura.NOTION_SENTINELA in comando


def test_curso_novo_sem_aulas_retorna_false_zero():
    ac = FakeNotionApp(lessons=[])          # Notion vazio p/ este prefixo
    assert captura.curso_ja_no_notion(_proj(), ac, CURSO) == (False, 0)


def test_prefixo_casa_apesar_de_query_string_na_url_do_curso():
    # A URL cadastrada pode vir com '?access_source=...'; as aulas NAO tem essa query.
    # Normalizar (tirar a query) ANTES do prefixo e o que faz casar. TEETH (b): se o
    # codigo nao tirar a query, o prefixo carrega '?...' e nada casa -> (False,0).
    ac = FakeNotionApp(lessons=AULAS)
    curso_com_query = CURSO + "?access_source=hero&utm=x"
    tem, qtd = captura.curso_ja_no_notion(_proj(), ac, curso_com_query)
    assert tem is True and qtd == 3


def test_curso_de_url_parecida_mas_distinta_nao_da_falso_positivo():
    # Curso 3486759 vs 34867590 (um digito a mais). Sem o limite de fronteira '/',
    # o prefixo '3486759' casaria '34867590/content/...' por engano. TEETH do prefixo.
    outro = "https://hotmart.com/pt-br/marketplace/produtos/x/products/34867590"
    aulas_do_outro = [outro + "/content/ZZZ", outro + "/content/YYY"]
    ac = FakeNotionApp(lessons=aulas_do_outro)
    assert captura.curso_ja_no_notion(_proj(), ac, CURSO) == (False, 0)


def test_ja_no_notion_sem_sentinela_levanta_honesto():
    # Saida sem a sentinela (ex.: traceback) NAO pode virar '(False,0)' silencioso —
    # isso viraria 'curso novo' e re-capturaria. Tem de LEVANTAR (o coordenar escala).
    ac = FakeNotionApp(saida="Traceback (most recent call last): boom")
    with pytest.raises(Exception):
        captura.curso_ja_no_notion(_proj(), ac, CURSO)


def test_ja_no_notion_sem_app_container_levanta():
    # Sem container de app nao da p/ verificar; nao pode assumir 'novo' (re-captura).
    with pytest.raises(Exception):
        captura.curso_ja_no_notion(_proj(app_container=""), FakeNotionApp(), CURSO)


# ==========================================================================
# GUARD em coordenar — nao enfileira curso ja capturado; enfileira o novo
# ==========================================================================
def test_guard_pula_curso_ja_capturado_COMPLETO_nao_enfileira():
    # Só pula quando o Notion PROVA completude: no_notion (485) >= total (485). O total
    # entra pela régua do gate I-1 (seam total_esperado_fn). Presença sozinha NÃO basta.
    checar = DoubleChecar(ja=[CURSO], quantidade=485)
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {}
    acao = captura.coordenar(_proj(), FakeNotionApp(), voz, executor=ex,
                             curso_url=CURSO, estado=estado, agora=AGORA,
                             ja_no_notion=checar, total_esperado_fn=lambda u: 485)
    assert ex.disparos == []                     # NAO enfileirou (ja COMPLETO)
    assert acao is not None and not acao.escalar
    assert estado["fase"] == captura.FASE_CONCLUIDO
    assert len(voz.avisos) == 1                  # reportou o skip via voz
    assert "485" in acao.descricao or "capturad" in acao.descricao.lower()
    assert checar.chamadas == [CURSO]


def test_guard_NAO_pula_curso_parcial_retoma_a_captura():
    # DENTES do bug central (achados [1]/[2]): 200/537 no Notion. PRESENÇA (200>0) NÃO
    # é completude. O guard antigo marcava CONCLUIDO e ABANDONAVA 337 aulas para sempre
    # (falso-pronto, viola Inviolável 4). Agora: NÃO conclui por presença -> RETOMA
    # (re-enfileira idempotente); o gate I-1 só declara pronto quando o Notion bater 537.
    checar = DoubleChecar(ja=[CURSO], quantidade=200)
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {}
    acao = captura.coordenar(_proj(), FakeNotionApp(), voz, executor=ex,
                             curso_url=CURSO, estado=estado, agora=AGORA,
                             ja_no_notion=checar, total_esperado_fn=lambda u: 537)
    assert estado.get("fase") != captura.FASE_CONCLUIDO   # NAO declarou pronto por presença
    assert estado.get("fase") == captura.FASE_CAPTURANDO   # retomou o protocolo de captura
    assert ex.disparos == [CURSO]                          # RE-ENFILEIROU (retomada)
    assert acao is not None and acao.executada and not acao.escalar
    # reportou a retomada (não o skip) — honesto sobre o parcial
    assert any("PARCIAL" in a.descricao or "RETOM" in a.descricao.upper()
               for a in voz.avisos)


def test_guard_total_desconhecido_falha_fechado_retoma_nao_pula():
    # DENTES do fail-closed (achado [6]): há aulas no Notion (qtd>0) mas o total esperado
    # é DESCONHECIDO (seam devolve 0 — ex.: course_id ainda não resolvido). NÃO pode
    # concluir por presença: o lado seguro é RE-CAPTURAR, nunca abandonar. Retoma.
    checar = DoubleChecar(ja=[CURSO], quantidade=200)
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {}
    captura.coordenar(_proj(), FakeNotionApp(), voz, executor=ex,
                      curso_url=CURSO, estado=estado, agora=AGORA,
                      ja_no_notion=checar, total_esperado_fn=lambda u: 0)
    assert estado.get("fase") != captura.FASE_CONCLUIDO
    assert ex.disparos == [CURSO]                          # retomou (fail-closed)


def test_guard_enfileira_curso_novo_normalmente():
    checar = DoubleChecar(ja=[])                 # nenhum curso capturado -> novo
    voz = FakeVoz()
    ex = FakeExecutor(confirmacao="enfileirado:42")
    estado = {}
    acao = captura.coordenar(_proj(), FakeNotionApp(), voz, executor=ex,
                             curso_url=CURSO, estado=estado, agora=AGORA,
                             ja_no_notion=checar)
    assert ex.disparos == [CURSO]                # enfileirou o novo
    assert acao.executada and estado["fase"] == captura.FASE_CAPTURANDO


def test_guard_nao_spamma_ciclo_a_ciclo_no_mesmo_estado():
    # Curso ja COMPLETO: reporta UMA vez e sossega nos ciclos seguintes do MESMO
    # processo (nao re-reporta a cada 120s). A DECISAO segue durAvel (Notion+total).
    checar = DoubleChecar(ja=[CURSO], quantidade=485)
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {}
    captura.coordenar(_proj(), FakeNotionApp(), voz, executor=ex, curso_url=CURSO,
                      estado=estado, agora=AGORA, ja_no_notion=checar,
                      total_esperado_fn=lambda u: 485)
    a2 = captura.coordenar(_proj(), FakeNotionApp(), voz, executor=ex, curso_url=CURSO,
                           estado=estado, agora=AGORA, ja_no_notion=checar,
                           total_esperado_fn=lambda u: 485)
    assert a2 is None                            # ciclo 2: quieto
    assert len(voz.avisos) == 1                  # so um aviso no total
    assert ex.disparos == []


def test_guard_falha_da_checagem_escala_e_nao_enfileira():
    # Se NAO da p/ consultar o Notion, o lado seguro e NAO enfileirar (evita re-captura
    # as cegas) e ESCALAR honesto; o proximo ciclo re-tenta (fase segue NOVO).
    def checar_boom(url):
        raise RuntimeError("notion off")
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {}
    acao = captura.coordenar(_proj(), FakeNotionApp(), voz, executor=ex,
                             curso_url=CURSO, estado=estado, agora=AGORA,
                             ja_no_notion=checar_boom)
    assert acao.escalar and not acao.executada
    assert ex.disparos == []
    assert estado.get("fase") != captura.FASE_CAPTURANDO
    assert len(voz.escaladas) == 1


# ==========================================================================
# RESILIENCIA / STATELESSNESS — durAvel (Notion), sobrevive a restart
# ==========================================================================
def test_resiliencia_restart_reinstancia_e_ainda_pula_pela_verdade_do_notion():
    # Instancia A ja pulou o curso COMPLETO (fase virou CONCLUIDO no estado in-process).
    checar = DoubleChecar(ja=[CURSO], quantidade=485)
    voz = FakeVoz()
    ex = FakeExecutor()
    estado_a = {}
    captura.coordenar(_proj(), FakeNotionApp(), voz, executor=ex, curso_url=CURSO,
                      estado=estado_a, agora=AGORA, ja_no_notion=checar,
                      total_esperado_fn=lambda u: 485)
    assert ex.disparos == []

    # RESTART: o Mac desligou / a sessao acabou -> o estado in-process se PERDE.
    # A nova instancia comeca com estado VAZIO (fase volta a NOVO). Se dependesse de
    # memoria local, re-enfileiraria. Como deriva de Notion+total, ainda pula (completo).
    estado_b = {}                                # <- fresh, como apos um restart
    acao = captura.coordenar(_proj(), FakeNotionApp(), voz, executor=ex, curso_url=CURSO,
                             estado=estado_b, agora=AGORA, ja_no_notion=checar,
                             total_esperado_fn=lambda u: 485)
    assert ex.disparos == []                     # continua NAO enfileirando (completo)
    assert acao is not None and not acao.escalar
    # provou statelessness: o skip veio da PROVA de completude (Notion+total), nao do
    # estado antigo.
    assert CURSO in checar.chamadas


def test_coordenar_usa_curso_ja_no_notion_por_padrao_quando_seam_omitido():
    # Sem injetar o seam ja_no_notion, coordenar cai no default real (curso_ja_no_notion
    # via exec_app). Aqui o Notion tem as 3 aulas e o total esperado é 3 -> COMPLETO ->
    # pula, provando o wiring do default. (total via seam para não depender do tracker.)
    ac = FakeNotionApp(lessons=AULAS)
    voz = FakeVoz()
    ex = FakeExecutor()
    estado = {}
    acao = captura.coordenar(_proj(), ac, voz, executor=ex, curso_url=CURSO,
                             estado=estado, agora=AGORA,       # <- sem ja_no_notion
                             total_esperado_fn=lambda u: 3)
    assert ex.disparos == []                     # pulou pelo default real (completo)
    assert ac.comandos and captura.NOTION_SENTINELA in ac.comandos[0][1]
