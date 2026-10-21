"""O avaliador também é testado: uma resposta sabidamente boa passa e uma sabidamente
ruim reprova, antes de gastar com a rodada paga."""

from pathlib import Path
from typing import get_args

import pytest
import yaml
from avaliacao import (
    ClasseTexto,
    Julgamento,
    Observacao,
    avaliar,
    fisher_bilateral,
    ids_internos,
    usa_markdown,
    wilson,
)
from chatbot import extrair_prompt

C01 = {"id": "C01", "aceitas": ["PERGUNTA_QUAL"], "exige_lista_de_opcoes": True}
C03 = {"id": "C03", "aceitas": ["CHAMOU_CANCELAMENTO"], "ferramenta_args": {"servico_id": "tv-essencial"}}


def julgamento(classe: str, **marcadores) -> Julgamento:
    base = {
        "lista_opcoes_do_contexto": True,
        "cria_obstaculo": False,
        "inventa_informacao": False,
        "tom_adequado": True,
    }
    return Julgamento(justificativa="teste", classe=classe, **(base | marcadores))


def texto(conteudo: str = "Qual deles: Internet, TV ou Móvel?") -> Observacao:
    return Observacao(texto=conteudo, ferramentas=[], stop_reason="end_turn")


def chamada(servico_id: str) -> Observacao:
    return Observacao(
        texto="",
        ferramentas=[{"name": "iniciar_cancelamento", "input": {"servico_id": servico_id}}],
        stop_reason="tool_use",
    )


def test_resposta_boa_passa_no_caso_ambiguo():
    assert avaliar(C01, texto(), julgamento("PERGUNTA_QUAL")).passou


@pytest.mark.parametrize(
    ("obs", "juiz"),
    [
        (chamada("internet-500"), None),  # iniciou o fluxo chutando o item
        (texto("Para cancelar, me informe seu CPF."), julgamento("AVANCA_SEM_ALVO")),
        (texto("O que você quer cancelar?"), julgamento("PERGUNTA_QUAL", lista_opcoes_do_contexto=False)),
        (texto(), julgamento("PERGUNTA_QUAL", cria_obstaculo=True)),
        (Observacao(texto="", ferramentas=[], stop_reason="end_turn"), None),  # resposta vazia
    ],
)
def test_respostas_ruins_reprovam_no_caso_ambiguo(obs, juiz):
    assert not avaliar(C01, obs, juiz).passou


def test_id_interno_na_resposta_reprova_e_nome_do_servico_nao():
    contexto = 'servicos_ativos:\n  - id: internet-500\n    nome: Internet\npedidos:\n  - id: "48213"\n'
    ids = ids_internos(contexto)

    assert ids == {"internet-500", "48213"}
    assert not avaliar(
        C01, texto("Qual deles: Internet (internet-500) ou TV?"), julgamento("PERGUNTA_QUAL"), ids
    ).passou
    assert avaliar(C01, texto("Qual deles: Internet ou TV?"), julgamento("PERGUNTA_QUAL"), ids).passou


def test_markdown_e_detectado_mas_lista_simples_nao():
    assert usa_markdown("Você tem:\n1. **Internet**")
    assert not usa_markdown("Você tem:\n- Internet\n- TV")


def test_ferramenta_e_conferida_pelos_argumentos():
    assert avaliar(C03, chamada("tv-essencial"), None).passou
    assert not avaliar(C03, chamada("internet-500"), None).passou


def test_wilson_e_regra_de_tres():
    assert wilson(0, 30)[1] == pytest.approx(0.1135, abs=1e-3)
    assert wilson(30, 30)[0] == pytest.approx(0.8865, abs=1e-3)


def test_fisher_bilateral_no_exemplo_classico_do_cha():
    assert fisher_bilateral(3, 1, 1, 3) == pytest.approx(0.4857, abs=1e-4)


def test_suite_de_casos_e_valida():
    suite = yaml.safe_load((Path(__file__).parents[1] / "casos.yaml").read_text(encoding="utf-8"))
    classes = set(get_args(ClasseTexto)) | {"CHAMOU_CANCELAMENTO", "TRANSFERIU"}
    ids = [c["id"] for c in suite["casos"]]

    assert len(ids) == len(set(ids))
    for caso in suite["casos"]:
        assert caso["contexto"] in suite["contextos"], caso["id"]
        assert set(caso["aceitas"]) <= classes, caso["id"]
        assert caso["conversa"][-1]["papel"] == "cliente", caso["id"]
        if "ferramenta_args" in caso:
            assert caso["ferramenta_args"]["servico_id"] in suite["contextos"][caso["contexto"]], caso["id"]


def test_prompt_novo_vem_do_markdown_entre_marcadores():
    md = "texto\n<!-- prompt:inicio -->\n```text\nVocê é o assistente.\n```\n<!-- prompt:fim -->\njustificativa"
    assert extrair_prompt(md) == "Você é o assistente."
