import io
import json
import time
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

import codigo_corrigido as cc

FILA = "arn:aws:sqs:us-east-1:123456789012:chat-entrada"


class FakeBedrock:
    def __init__(self, falhar_em: set[str] = frozenset()) -> None:
        self.chamadas: list[dict] = []
        self._falhar_em = falhar_em

    def converse(self, **params):
        self.chamadas.append(params)
        texto = params["messages"][-1]["content"][0]["text"]
        if texto in self._falhar_em:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}}, "Converse")
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": f"resposta para: {texto}"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150},
            "metrics": {"latencyMs": 900},
        }


class FakeDynamo:
    def __init__(self) -> None:
        self.itens: list[dict] = []

    def query(self, ExpressionAttributeValues, Limit, **_):
        sessao = ExpressionAttributeValues[":s"]["S"]
        da_sessao = sorted((i for i in self.itens if i["session_id"]["S"] == sessao), key=lambda i: i["sk"]["S"])
        return {"Items": list(reversed(da_sessao))[:Limit]}

    def put_item(self, Item, **_):
        self.itens.append(Item)


class FakeSqs:
    def __init__(self) -> None:
        self.enviadas: list[dict] = []

    def send_message(self, QueueUrl, MessageBody):
        self.enviadas.append(json.loads(MessageBody))


@pytest.fixture
def aws(monkeypatch):
    fakes = SimpleNamespace(bedrock=FakeBedrock(), dynamodb=FakeDynamo(), sqs=FakeSqs())
    for nome in ("bedrock", "dynamodb", "sqs"):
        monkeypatch.setattr(cc, nome, getattr(fakes, nome))
    return fakes


def contexto(restante_ms: int = 60_000) -> SimpleNamespace:
    return SimpleNamespace(get_remaining_time_in_millis=lambda: restante_ms)


def registro(message_id: str, sessao: str = "s1", texto: str = "oi", body: str | None = None, fila: str = FILA):
    return {
        "messageId": message_id,
        "receiptHandle": f"rh-{message_id}",
        "body": body if body is not None else json.dumps({"session_id": sessao, "message": texto}),
        "attributes": {"ApproximateReceiveCount": "1", "SentTimestamp": str(int(time.time() * 1000))},
        "eventSourceARN": fila,
    }


def falhas(resultado) -> list[str]:
    return [f["itemIdentifier"] for f in resultado["batchItemFailures"]]


def test_so_a_mensagem_com_erro_volta_para_a_fila(aws):
    aws.bedrock = FakeBedrock(falhar_em={"quero cancelar"})
    cc.bedrock = aws.bedrock
    evento = {
        "Records": [registro("m1", "s1", "oi"), registro("m2", "s2", "quero cancelar"), registro("m3", "s3", "boleto")]
    }

    resultado = cc.handler(evento, contexto())

    assert falhas(resultado) == ["m2"]
    assert [m["session_id"] for m in aws.sqs.enviadas] == ["s1", "s3"]


def test_mensagem_malformada_falha_sozinha(aws):
    evento = {"Records": [registro("m1", body="isto não é json"), registro("m2", "s2", "tudo bem?")]}

    resultado = cc.handler(evento, contexto())

    assert falhas(resultado) == ["m1"]
    assert [m["in_reply_to"] for m in aws.sqs.enviadas] == ["m2"]


def test_fila_fifo_para_na_primeira_falha_e_devolve_o_resto(aws):
    fifo = FILA + ".fifo"
    evento = {
        "Records": [
            registro("m1", "s1", fila=fifo),
            registro("m2", body="{", fila=fifo),
            registro("m3", "s3", fila=fifo),
        ]
    }

    resultado = cc.handler(evento, contexto())

    assert falhas(resultado) == ["m2", "m3"]
    assert len(aws.bedrock.chamadas) == 1


def test_sem_tempo_para_o_modelo_a_mensagem_volta_sem_estourar_o_timeout(aws):
    pouco = cc.MODEL_CALL_BUDGET_MS - 1

    resultado = cc.handler({"Records": [registro("m1"), registro("m2", "s2")]}, contexto(pouco))

    assert falhas(resultado) == ["m1", "m2"]
    assert aws.bedrock.chamadas == []


def test_historico_e_system_prompt_vao_para_o_modelo(aws):
    cc.handler({"Records": [registro("m1", "s1", "meu pedido é o 123")]}, contexto())

    cc.handler({"Records": [registro("m2", "s1", "quero cancelar ele")]}, contexto())

    chamada = aws.bedrock.chamadas[-1]
    assert chamada["system"] == [{"text": cc.SYSTEM_PROMPT}]
    assert [(m["role"], m["content"][0]["text"]) for m in chamada["messages"]] == [
        ("user", "meu pedido é o 123"),
        ("assistant", "resposta para: meu pedido é o 123"),
        ("user", "quero cancelar ele"),
    ]


def test_reentrega_reenvia_a_resposta_gravada_sem_chamar_o_modelo(aws):
    cc.handler({"Records": [registro("m1", "s1", "qual meu saldo?")]}, contexto())

    resultado = cc.handler({"Records": [registro("m1", "s1", "qual meu saldo?")]}, contexto())

    assert falhas(resultado) == []
    assert len(aws.bedrock.chamadas) == 1
    assert aws.sqs.enviadas[0] == aws.sqs.enviadas[1]


def test_log_estruturado_tem_os_campos_e_nao_tem_o_texto_da_conversa(aws, monkeypatch):
    saida = io.StringIO()
    monkeypatch.setattr(cc.logger.handlers[0], "stream", saida)

    cc.handler({"Records": [registro("m1", "s1", "segredo do cliente")]}, contexto())

    linha = json.loads(saida.getvalue().strip().splitlines()[-1])

    assert linha["event"] == "mensagem_processada"
    assert {"session_id", "message_id", "queue_age_ms", "model_latency_ms", "input_tokens", "output_tokens"} <= set(
        linha
    )
    assert "segredo do cliente" not in json.dumps(linha, ensure_ascii=False)


def test_chamadas_aws_seguem_o_formato_real_das_apis(monkeypatch):
    """O Stubber valida cada chamada contra o modelo da API no botocore, sem acessar a AWS."""
    clientes = {
        nome: boto3.client(servico)
        for nome, servico in [("bedrock", "bedrock-runtime"), ("dynamodb", "dynamodb"), ("sqs", "sqs")]
    }
    for nome, cliente in clientes.items():
        monkeypatch.setattr(cc, nome, cliente)
    stubs = {nome: Stubber(cliente) for nome, cliente in clientes.items()}

    stubs["dynamodb"].add_response(
        "query",
        {"Items": []},
        {
            "TableName": "chat-sessoes",
            "KeyConditionExpression": "session_id = :s",
            "ExpressionAttributeValues": {":s": {"S": "s1"}},
            "ScanIndexForward": False,
            "Limit": cc.MAX_HISTORY_TURNS,
            "ConsistentRead": True,
        },
    )
    stubs["bedrock"].add_response(
        "converse",
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "Qual serviço você quer cancelar?"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1500, "outputTokens": 12, "totalTokens": 1512},
            "metrics": {"latencyMs": 640},
        },
        {
            "modelId": "global.anthropic.claude-haiku-5-5",
            "system": [{"text": cc.SYSTEM_PROMPT}],
            "messages": [{"role": "user", "content": [{"text": "quero cancelar"}]}],
            "inferenceConfig": {"maxTokens": cc.MAX_TOKENS},
        },
    )
    stubs["dynamodb"].add_response("put_item", {}, {"TableName": "chat-sessoes", "Item": ANY})
    stubs["sqs"].add_response(
        "send_message",
        {"MessageId": "r1", "MD5OfMessageBody": "x"},
        {"QueueUrl": cc.REPLY_QUEUE_URL, "MessageBody": ANY},
    )

    for stub in stubs.values():
        stub.activate()
    resultado = cc.handler({"Records": [registro("m1", "s1", "quero cancelar")]}, contexto())

    assert falhas(resultado) == []
    for stub in stubs.values():
        stub.assert_no_pending_responses()
