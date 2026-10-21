"""Reproduz os sintomas do enunciado no código original e compara com o corrigido.

Os dois handlers rodam com dublês do Bedrock, do DynamoDB e do SQS (nada fala com a AWS)
e um relógio simulado: cada chamada ao modelo "leva" a latência do cenário, e a Lambda é
encerrada quando o relógio passa do timeout da função.

    python exercicio-1/simulacao.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError

TIMEOUT_FUNCAO_S = 60
MAX_RECEIVE_COUNT = 5

_prompt = Path(tempfile.mkdtemp()) / "prompt.txt"
_prompt.write_text("Você é o assistente de suporte.", encoding="utf-8")
os.environ.update(
    {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "simulacao",
        "AWS_SECRET_ACCESS_KEY": "simulacao",
        "MODEL_ID": "modelo-simulado",
        "SESSION_TABLE": "chat-sessoes",
        "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/chat-respostas",
        "SYSTEM_PROMPT_PATH": str(_prompt),
    }
)
sys.path.insert(0, str(Path(__file__).parent))

import codigo_corrigido as cc  # noqa: E402
import codigo_original  # noqa: E402

cc.logger.disabled = True


class TimeoutDaFuncao(BaseException):
    """O runtime da Lambda encerra a invocação: o lote inteiro volta para a fila.

    É BaseException porque o handler não tem como capturar o fim da invocação; como Exception,
    o try/except por mensagem do corrigido engoliria o timeout.
    """


class Modelo:
    """Dublê do Bedrock com relógio simulado. Atende o original (invoke_model) e o corrigido (converse)."""

    def __init__(self, latencia_s: float, falhar_em: set[str] = frozenset()) -> None:
        self.latencia_s = latencia_s
        self.falhar_em = falhar_em
        self.agora_s = 0.0
        self.concluidas = 0

    def _gerar(self, texto: str) -> str:
        if self.agora_s + self.latencia_s > TIMEOUT_FUNCAO_S:
            self.agora_s = TIMEOUT_FUNCAO_S
            raise TimeoutDaFuncao
        self.agora_s += self.latencia_s
        if texto in self.falhar_em:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}}, "Converse")
        self.concluidas += 1
        return f"resposta para: {texto}"

    def invoke_model(self, body: str, **_: object) -> dict:
        resposta = self._gerar(json.loads(body)["messages"][0]["content"])
        return {"body": io.BytesIO(json.dumps({"content": [{"type": "text", "text": resposta}]}).encode())}

    def converse(self, messages: list, **_: object) -> dict:
        resposta = self._gerar(messages[-1]["content"][0]["text"])
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": resposta}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
            "metrics": {"latencyMs": int(self.latencia_s * 1000)},
        }

    def contexto(self) -> SimpleNamespace:
        return SimpleNamespace(get_remaining_time_in_millis=lambda: int((TIMEOUT_FUNCAO_S - self.agora_s) * 1000))


class Historico:
    def __init__(self) -> None:
        self.itens: list[dict] = []

    def query(self, ExpressionAttributeValues: dict, Limit: int, **_: object) -> dict:
        sessao = ExpressionAttributeValues[":s"]["S"]
        return {"Items": [i for i in self.itens if i["session_id"]["S"] == sessao][-Limit:][::-1]}

    def put_item(self, Item: dict, **_: object) -> None:
        self.itens.append(Item)


class FilaDeRespostas:
    def __init__(self) -> None:
        self.enviadas = 0

    def send_message(self, **_: object) -> None:
        self.enviadas += 1


def lote(malformada: int | None = None) -> list[dict]:
    records = []
    for i in range(10):
        body = "{json quebrado" if i == malformada else json.dumps({"session_id": f"s{i}", "message": f"pergunta {i}"})
        records.append({"messageId": f"m{i}", "body": body, "eventSourceARN": "arn:aws:sqs:us-east-1:1:chat-entrada"})
    return records


@dataclass
class Resultado:
    chamadas_ao_modelo: int = 0
    chamadas_perdidas: int = 0
    devolvidas: int = 0
    respostas_entregues: int = 0
    validas_na_dlq: int = 0
    timeout: str = "não"


def rodar_original(records: list[dict], latencia_s: float, falhar_em: set[str] = frozenset(), tentativas: int = 1):
    r = Resultado()
    for _ in range(tentativas):
        modelo = Modelo(latencia_s, falhar_em)
        clientes = {"bedrock-runtime": modelo, "sqs": object()}
        with patch("boto3.client", side_effect=clientes.__getitem__), redirect_stdout(io.StringIO()):
            try:
                codigo_original.handler({"Records": records}, None)
                falhou = False
            except TimeoutDaFuncao:
                falhou, r.timeout = True, "sim"
            except Exception:
                falhou = True
        r.chamadas_ao_modelo += modelo.concluidas
        if falhou:
            # O lote inteiro volta, inclusive as mensagens que o modelo já tinha respondido.
            r.chamadas_perdidas += modelo.concluidas
            r.devolvidas = len(records)
    if r.devolvidas and tentativas >= MAX_RECEIVE_COUNT:
        r.validas_na_dlq = sum(1 for rec in records if rec["body"].startswith('{"'))
    return r  # o original só faz print: nenhuma resposta chega ao usuário


def rodar_corrigido(records: list[dict], latencia_s: float, falhar_em: set[str] = frozenset(), tentativas: int = 1):
    r = Resultado()
    historico, pendentes = Historico(), records
    for _ in range(tentativas):
        modelo, fila, gravados = Modelo(latencia_s, falhar_em), FilaDeRespostas(), len(historico.itens)
        cc.bedrock, cc.dynamodb, cc.sqs = modelo, historico, fila
        try:
            resposta = cc.handler({"Records": pendentes}, modelo.contexto())
            falhas = {f["itemIdentifier"] for f in resposta["batchItemFailures"]}
        except TimeoutDaFuncao:
            falhas, r.timeout = {rec["messageId"] for rec in pendentes}, "sim"
        r.chamadas_ao_modelo += modelo.concluidas
        # Perdida é a resposta gerada e não gravada: na reentrega, o modelo seria chamado de novo.
        r.chamadas_perdidas += modelo.concluidas - (len(historico.itens) - gravados)
        r.respostas_entregues += fila.enviadas
        r.devolvidas = len(falhas)
        pendentes = [rec for rec in pendentes if rec["messageId"] in falhas]
    if pendentes and tentativas >= MAX_RECEIVE_COUNT:
        r.validas_na_dlq = sum(1 for rec in pendentes if rec["body"].startswith('{"'))
    return r


def imprimir(titulo: str, original: Resultado, corrigido: Resultado) -> None:
    print(f"\n{titulo}")
    print(f"  {'':40}{'original':>10}{'corrigido':>11}")
    linhas = [
        ("chamadas ao modelo", "chamadas_ao_modelo"),
        ("chamadas perdidas (lote devolvido)", "chamadas_perdidas"),
        ("mensagens devolvidas à fila", "devolvidas"),
        ("respostas entregues ao usuário", "respostas_entregues"),
        ("mensagens válidas na DLQ", "validas_na_dlq"),
        ("estourou o timeout da função", "timeout"),
    ]
    for nome, campo in linhas:
        print(f"  {nome:40}{getattr(original, campo)!s:>10}{getattr(corrigido, campo)!s:>11}")


def main() -> None:
    print(f"Lote de 10 mensagens de sessões diferentes, timeout da função de {TIMEOUT_FUNCAO_S} s.")
    imprimir(
        "A. Pico: o modelo leva 8 s por resposta",
        rodar_original(lote(), latencia_s=8),
        rodar_corrigido(lote(), latencia_s=8),
    )
    imprimir(
        "B. Throttling do Bedrock na 3ª mensagem (2 s por resposta)",
        rodar_original(lote(), latencia_s=2, falhar_em={"pergunta 2"}),
        rodar_corrigido(lote(), latencia_s=2, falhar_em={"pergunta 2"}),
    )
    imprimir(
        f"C. Uma mensagem malformada no lote, após {MAX_RECEIVE_COUNT} tentativas (2 s por resposta)",
        rodar_original(lote(malformada=4), latencia_s=2, tentativas=MAX_RECEIVE_COUNT),
        rodar_corrigido(lote(malformada=4), latencia_s=2, tentativas=MAX_RECEIVE_COUNT),
    )
    print(
        "\nNo cenário A, o corrigido não estoura o timeout nem joga resposta fora: responde o que cabe"
        "\nno prazo e devolve o resto, sem chamar o modelo para ele. Para o lote inteiro caber, o"
        "\nBatchSize do event source mapping precisa acompanhar a latência do pico (diagnostico.md)."
    )


if __name__ == "__main__":
    main()
