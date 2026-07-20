"""Capacidade C (parte ORQUESTRAÇÃO) — a Athena OPERA o Sintetizador no fluxo
pós-captura. Dado um curso classificado como how-to (SINAL injetável), ela dispara
o Sintetizador (seam injetável, que no real roda o pacote `sintetizador/` do motor),
registra o artefato gerado (skill+agente+sistema) e o faz PASSAR pelo Portão POP —
só declarando 'sintetizado' depois que o portão limpou (mata o falso-pronto).

O que estes testes exercem é a LÓGICA DO ORQUESTRADOR — não o sintetizador, nem o
review/fix do Portão. Tudo é COSTURA injetável (callables); os dublês aqui MODELAM
o mecanismo (classificação how-to/info, síntese que devolve artefato, portão que
aprova/reprova, registro) para provar as invariantes:

  1. curso how-to  -> ACIONA o sintetizador; curso info -> NÃO aciona (nem toca
     portão/registro).
  2. o artefato só é REGISTRADO depois que o Portão POP limpou — nunca antes
     (o falso-pronto que o dono odeia).
  3. portão reprova -> não registra, `sintetizado=False`, escala pro humano.
  4. `sintetizado=True` exige portão limpo + registro efetivado.

Os testes marcados 'DENTES' foram provados: reintroduzi cada bug (sempre sintetiza;
registra antes do portão; declara pronto sem checar o portão) e confirmei que o
teste correspondente FALHA contra o loop ingênuo."""
from maestro import sintetizador_orq
from maestro.sintetizador_orq import Artefato


# --- Dublês que MODELAM o mecanismo ----------------------------------------
class Classificador:
    """Modela o SINAL de classificação (injetável). `how_to` fixa a resposta;
    registra os cursos vistos."""
    def __init__(self, how_to):
        self._how_to = bool(how_to)
        self.vistos = []

    def __call__(self, curso):
        self.vistos.append(curso)
        return self._how_to


class Sintetizador:
    """Modela o pacote `sintetizador/`: dado o curso, devolve um Artefato
    (skill+agente+sistema). É o SPY que prova aciona / não-aciona."""
    def __init__(self, artefato=None, log=None):
        self._artefato = artefato or Artefato(
            skill="skill-x", agente="agente-x", sistema="sistema-x")
        self.cursos = []
        self._log = log

    def __call__(self, curso):
        self.cursos.append(curso)
        if self._log is not None:
            self._log.append("sintetizar")
        return self._artefato


class PortaoDuble:
    """Modela o Portão POP visto pelo orquestrador: um seam que recebe o artefato
    e devolve um resultado com `.pronto`/`.escalar`. SPY do que passou pelo portão."""
    def __init__(self, pronto=True, log=None):
        self._pronto = bool(pronto)
        self.artefatos = []
        self._log = log

    def __call__(self, artefato):
        self.artefatos.append(artefato)
        if self._log is not None:
            self._log.append("portao")
        return sintetizador_orq.ResultadoPortao(
            pronto=self._pronto, escalar=not self._pronto)


class Registro:
    """Modela o registro do artefato gerado. SPY que prova 'só registra após o
    portão limpar'."""
    def __init__(self, log=None):
        self.registrados = []
        self._log = log

    def __call__(self, artefato):
        self.registrados.append(artefato)
        if self._log is not None:
            self._log.append("registrar")


def _orquestrar(*, how_to, portao_pronto=True, artefato=None, log=None):
    """Helper: monta as costuras e roda o orquestrador."""
    clas = Classificador(how_to)
    sint = Sintetizador(artefato=artefato, log=log)
    port = PortaoDuble(pronto=portao_pronto, log=log)
    reg = Registro(log=log)
    estado = sintetizador_orq.orquestrar_sintese(
        "curso-x", classificar=clas, sintetizar=sint, portao=port, registrar=reg)
    return estado, clas, sint, port, reg


# --- 1. curso how-to ACIONA o sintetizador ---------------------------------
def test_curso_how_to_aciona_sintetizador():
    estado, clas, sint, port, reg = _orquestrar(how_to=True)
    assert sint.cursos == ["curso-x"]        # acionou (uma vez, com o curso)
    assert estado.how_to is True
    assert estado.sintetizado is True
    assert estado.escalar is False


# --- 2. DENTES: curso info NÃO aciona (nem toca portão/registro) ------------
def test_dentes_curso_info_nao_aciona_sintetizador():
    """DENTES: reintroduzi 'sempre sintetiza' (ignora a classificação). Contra esse
    orquestrador ingênuo o Sintetizador SERIA chamado pro curso info; aqui exigimos
    que NÃO seja — e que portão e registro nem sejam tocados."""
    estado, clas, sint, port, reg = _orquestrar(how_to=False)
    assert sint.cursos == []                 # NÃO acionou
    assert port.artefatos == []              # não tocou o portão
    assert reg.registrados == []             # não registrou
    assert estado.how_to is False
    assert estado.sintetizado is False
    assert estado.artefato is None
    assert estado.escalar is False           # não-how-to não é problema, é normal


# --- 3. o artefato registrado carrega skill + agente + sistema -------------
def test_artefato_registrado_carrega_skill_agente_sistema():
    art = Artefato(skill="s1", agente="a1", sistema="sis1")
    estado, clas, sint, port, reg = _orquestrar(how_to=True, artefato=art)
    assert estado.artefato == art
    assert estado.artefato.skill == "s1"
    assert estado.artefato.agente == "a1"
    assert estado.artefato.sistema == "sis1"
    assert reg.registrados == [art]          # registrou o artefato gerado
    assert estado.registrado is True


# --- 4. DENTES: portão reprova -> não registra, escala (mata o falso-pronto) -
def test_dentes_portao_reprova_nao_registra_e_escala():
    """DENTES: reintroduzi 'declara sintetizado logo após a síntese' (registra sem
    checar o portão). Com o portão REPROVANDO, esse loop ingênuo registraria e diria
    sintetizado=True; aqui exigimos NÃO registrar, sintetizado=False e escalar."""
    art = Artefato(skill="s", agente="a", sistema="sis")
    estado, clas, sint, port, reg = _orquestrar(
        how_to=True, portao_pronto=False, artefato=art)
    assert sint.cursos == ["curso-x"]        # sintetizou (o portão é depois)
    assert port.artefatos == [art]           # o artefato PASSOU pelo portão
    assert reg.registrados == []             # mas NÃO foi registrado (reprovou)
    assert estado.sintetizado is False
    assert estado.registrado is False
    assert estado.escalar is True
    assert estado.artefato == art            # o artefato reprovado fica no estado


# --- 5. DENTES: o portão roda ANTES do registro ----------------------------
def test_dentes_portao_roda_antes_de_registrar():
    """DENTES: reintroduzi 'registra antes de passar pelo portão'. A ordem no log
    revela isso: exigimos portão ANTES de registrar (nunca se registra um artefato
    não-aprovado)."""
    log = []
    estado, clas, sint, port, reg = _orquestrar(how_to=True, log=log)
    assert log == ["sintetizar", "portao", "registrar"]
    assert log.index("portao") < log.index("registrar")


# --- 6. sintetizado=True exige portão limpo + registro ---------------------
def test_sintetizado_so_apos_portao_limpo_e_registro():
    estado, clas, sint, port, reg = _orquestrar(how_to=True, portao_pronto=True)
    assert estado.sintetizado is True
    assert estado.registrado is True
    assert estado.escalar is False
    assert "portão" in estado.motivo.lower() or "pop" in estado.motivo.lower()


# --- 7. fail-closed: síntese sem artefato escala, não registra -------------
def test_sintese_sem_artefato_escala_e_nao_registra():
    """Fail-closed: se o Sintetizador não devolve artefato (falha silenciosa), o
    orquestrador NÃO chama o portão nem registra, e escala — nunca declara pronto
    com base em nada."""
    class SemArtefato:
        def __init__(self):
            self.cursos = []

        def __call__(self, curso):
            self.cursos.append(curso)
            return None

    sint = SemArtefato()
    port = PortaoDuble(pronto=True)
    reg = Registro()
    estado = sintetizador_orq.orquestrar_sintese(
        "curso-x", classificar=Classificador(True),
        sintetizar=sint, portao=port, registrar=reg)
    assert sint.cursos == ["curso-x"]
    assert port.artefatos == []              # não tocou o portão (nada a revisar)
    assert reg.registrados == []             # não registrou
    assert estado.sintetizado is False
    assert estado.escalar is True


# --- 8. DENTES: registro falha APÓS portão limpo -> fail-closed, escala ------
def test_dentes_registro_falha_apos_portao_e_fail_closed():
    """DENTES: reintroduzi 'registrar sem guarda' (a chamada crua da linha 102).

    O `registrar` real é I/O (Notion/DB) que FALHA na prática. Contra o
    orquestrador ingênuo — que não embrulha o registro — uma exceção do registro
    ESCAPA como crash: o curso já passou pelo portão limpo mas NÃO produz nenhum
    EstadoSintese nem registro de escalonamento — o limbo exato que o fail-closed
    existe pra matar (todos os OUTROS caminhos de falha devolvem escalar=True).

    Aqui exigimos: a exceção NÃO estoura; devolve EstadoSintese auditável com
    sintetizado=False (registro não efetivado -> invariante 4), registrado=False,
    escalar=True, artefato preservado pra auditoria. Contra o código antigo este
    teste ERRA com a exceção do registro (não devolve estado)."""
    art = Artefato(skill="s", agente="a", sistema="sis")

    class RegistroQueFalha:
        def __init__(self):
            self.tentativas = []

        def __call__(self, artefato):
            self.tentativas.append(artefato)
            raise RuntimeError("registry down (Notion/DB indisponível)")

    reg = RegistroQueFalha()
    port = PortaoDuble(pronto=True)
    sint = Sintetizador(artefato=art)
    estado = sintetizador_orq.orquestrar_sintese(
        "curso-x", classificar=Classificador(True),
        sintetizar=sint, portao=port, registrar=reg)
    assert reg.tentativas == [art]           # tentou registrar (portão já limpou)
    assert estado.sintetizado is False       # registro não efetivou -> invariante 4
    assert estado.registrado is False
    assert estado.escalar is True            # fail-closed: humano decide
    assert estado.artefato == art            # artefato preservado pra auditoria
    assert "regist" in estado.motivo.lower()
