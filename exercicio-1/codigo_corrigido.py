"""Consumer SQS -> Amazon Bedrock do chatbot, corrigido.

O que mudou em relação ao original (o porquê de cada item está em diagnostico.md):

1. Falha parcial de lote: cada mensagem tem o seu try/except e só as que falharam voltam
   para a fila, em batchItemFailures. Exige ReportBatchItemFailures no event source mapping.
2. Prazo da Lambda: antes de chamar o modelo, confere se a chamada cabe no tempo que resta.
   Se não cabe, a mensagem volta para a fila em vez de a função estourar o timeout.
3. Clientes criados uma vez, fora do handler, com timeout e número de tentativas explícitos.
4. Contexto da sessão: o histórico recente (DynamoDB) e o system prompt vão para o modelo,
   e a pergunta e a resposta são gravadas no histórico.
5. Reentrega: se a mensagem já foi respondida, reenvia a resposta gravada sem chamar o modelo.
6. A resposta é publicada na fila de respostas, e não só impressa.
7. Log estruturado em JSON, uma linha por mensagem, sem o texto da conversa.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

MODEL_ID = os.environ["MODEL_ID"]
SESSION_TABLE = os.environ["SESSION_TABLE"]
# Premissa: QUEUE_URL é a fila de respostas. O original cria o cliente SQS e lê essa
# variável sem usar; o mais provável é que a publicação da resposta tenha ficado de fora.
REPLY_QUEUE_URL = os.environ["QUEUE_URL"]
SYSTEM_PROMPT = Path(os.environ["SYSTEM_PROMPT_PATH"]).read_text(encoding="utf-8")
MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "10"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "600"))

CONNECT_TIMEOUT_S = 2
READ_TIMEOUT_S = 15
MAX_ATTEMPTS = 2
# Pior caso de uma chamada ao modelo, somando as tentativas do SDK, mais margem para
# gravar e publicar. A função só começa uma chamada se esse tempo ainda couber.
MODEL_CALL_BUDGET_MS = MAX_ATTEMPTS * (CONNECT_TIMEOUT_S + READ_TIMEOUT_S) * 1000 + 3000

bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(
        connect_timeout=CONNECT_TIMEOUT_S,
        read_timeout=READ_TIMEOUT_S,
        retries={"mode": "standard", "max_attempts": MAX_ATTEMPTS},
    ),
)
dynamodb = boto3.client("dynamodb")
sqs = boto3.client("sqs")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {"level": record.levelname, "event": record.getMessage(), **getattr(record, "fields", {})}
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False, default=str)


logger = logging.getLogger("chatbot")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(JsonFormatter())
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def log(level: int, event: str, exc_info: bool = False, **fields: Any) -> None:
    logger.log(level, event, exc_info=exc_info, extra={"fields": fields})


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    records = event["Records"]
    fifo = bool(records) and records[0]["eventSourceARN"].endswith(".fifo")
    failures: list[dict[str, str]] = []

    for i, record in enumerate(records):
        message_id = record["messageId"]
        if context.get_remaining_time_in_millis() < MODEL_CALL_BUDGET_MS:
            # Não dá tempo de chamar o modelo: esta e as próximas voltam para a fila,
            # sem estourar o timeout (que devolveria o lote inteiro).
            pending = records[i:]
            failures += [{"itemIdentifier": r["messageId"]} for r in pending]
            log(logging.WARNING, "prazo_insuficiente", devolvidas=len(pending))
            break
        try:
            result = process_record(record)
            log(logging.INFO, "mensagem_processada", sqs_message_id=message_id, **result)
        except Exception as exc:
            log(logging.ERROR, "falha_na_mensagem", exc_info=True, sqs_message_id=message_id, error=type(exc).__name__)
            failures.append({"itemIdentifier": message_id})
            if fifo:
                # Fila FIFO: a AWS orienta parar na primeira falha e devolver o resto,
                # para não responder uma mensagem antes da anterior.
                failures += [{"itemIdentifier": r["messageId"]} for r in records[i + 1 :]]
                break

    return {"batchItemFailures": failures}


def process_record(record: dict[str, Any]) -> dict[str, Any]:
    body = json.loads(record["body"])
    session_id, text = body["session_id"], body["message"]
    message_id = body.get("message_id") or record["messageId"]
    attributes = record.get("attributes", {})
    sent_ms = int(attributes.get("SentTimestamp", time.time() * 1000))
    fields = {
        "session_id": session_id,
        "message_id": message_id,
        "receive_count": int(attributes.get("ApproximateReceiveCount", 1)),
        "queue_age_ms": int(time.time() * 1000) - sent_ms,
    }

    history = load_history(session_id)
    previous = next((turn for turn in history if turn["message_id"] == message_id), None)
    if previous is not None:
        publish_reply(session_id, message_id, previous["assistant"])
        return {**fields, "status": "reenviada"}

    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[*to_messages(history), {"role": "user", "content": [{"text": text}]}],
        inferenceConfig={"maxTokens": MAX_TOKENS},
    )
    reply = "".join(block.get("text", "") for block in response["output"]["message"]["content"]).strip()
    if not reply:
        raise ValueError(f"resposta vazia do modelo (stopReason={response['stopReason']})")

    save_turn(session_id, message_id, sent_ms, text, reply)
    publish_reply(session_id, message_id, reply)
    usage = response["usage"]
    return {
        **fields,
        "status": "respondida",
        "model_latency_ms": response["metrics"]["latencyMs"],
        "input_tokens": usage["inputTokens"],
        "output_tokens": usage["outputTokens"],
        "stop_reason": response["stopReason"],
    }


def load_history(session_id: str) -> list[dict[str, str]]:
    """Últimos turnos da sessão, do mais antigo para o mais recente."""
    items = dynamodb.query(
        TableName=SESSION_TABLE,
        KeyConditionExpression="session_id = :s",
        ExpressionAttributeValues={":s": {"S": session_id}},
        ScanIndexForward=False,
        Limit=MAX_HISTORY_TURNS,
        ConsistentRead=True,
    )["Items"]
    turns = [{key: value["S"] for key, value in item.items() if "S" in value} for item in items]
    return list(reversed(turns))


def to_messages(history: list[dict[str, str]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in history:
        messages.append({"role": "user", "content": [{"text": turn["user"]}]})
        messages.append({"role": "assistant", "content": [{"text": turn["assistant"]}]})
    return messages


def save_turn(session_id: str, message_id: str, sent_ms: int, user: str, assistant: str) -> None:
    # Um item por turno: gravações de mensagens diferentes nunca se sobrescrevem, e a
    # chave ordena o histórico pela hora de envio.
    dynamodb.put_item(
        TableName=SESSION_TABLE,
        Item={
            "session_id": {"S": session_id},
            "sk": {"S": f"{sent_ms:013d}#{message_id}"},
            "message_id": {"S": message_id},
            "user": {"S": user},
            "assistant": {"S": assistant},
            # Histórico de conversa é dado pessoal: expira sozinho em 7 dias (TTL).
            "expires_at": {"N": str(int(time.time()) + 7 * 24 * 3600)},
        },
    )


def publish_reply(session_id: str, message_id: str, text: str) -> None:
    sqs.send_message(
        QueueUrl=REPLY_QUEUE_URL,
        MessageBody=json.dumps({"session_id": session_id, "in_reply_to": message_id, "text": text}, ensure_ascii=False),
    )
