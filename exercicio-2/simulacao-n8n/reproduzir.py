"""Reproduz a falha silenciosa do reprocessamento da DLQ num n8n de verdade (2.44.5).

Sobe com docker compose um SQS local (ElasticMQ), um /reprocess falso e o n8n, coloca 5
mensagens na DLQ (4 válidas e 1 malformada) e roda cada cenário pelo CLI do n8n. O que
conta é o efeito na fila, não a cor da execução.

    python exercicio-2/simulacao-n8n/reproduzir.py      (precisa de Docker; leva cerca de 2 min)
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import boto3

AQUI = Path(__file__).resolve().parent
DLQ, QUARENTENA = "chat-dlq", "chat-quarentena"
sqs = boto3.client(
    "sqs",
    endpoint_url="http://127.0.0.1:19324",
    region_name="us-east-1",
    aws_access_key_id="local",
    aws_secret_access_key="local",
)


def compose(*args: str, entrada: str | None = None) -> subprocess.CompletedProcess:
    # UTF-8 explícito: no Windows o padrão seria cp1252, e o workflow e a saída do n8n têm acento.
    return subprocess.run(["docker", "compose", *args], cwd=AQUI, input=entrada, capture_output=True, encoding="utf-8")


def url(fila: str) -> str:
    return sqs.get_queue_url(QueueName=fila)["QueueUrl"]


def mensagens_na_fila(fila: str) -> int:
    nomes = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
    atributos = sqs.get_queue_attributes(QueueUrl=url(fila), AttributeNames=nomes)["Attributes"]
    return sum(int(atributos[n]) for n in nomes)


def chamadas_ao_reprocess(metodo: str = "GET") -> list[dict]:
    return json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:18080/chamadas", method=metodo)))


def preparar() -> None:
    for fila in (DLQ, QUARENTENA):
        sqs.purge_queue(QueueUrl=url(fila))
    chamadas_ao_reprocess("DELETE")
    for i in range(4):
        corpo = json.dumps({"session_id": f"s{i}", "message": f"pergunta {i}"})
        sqs.send_message(QueueUrl=url(DLQ), MessageBody=corpo)
    sqs.send_message(QueueUrl=url(DLQ), MessageBody="{json quebrado")


def importar(workflow: dict) -> None:
    # Escrito pelo stdin, o arquivo nasce do usuário node. Com docker cp ele manteria o dono e o
    # modo 0600 do arquivo temporário, e no Mac (uid 501) o n8n não conseguiria ler.
    conteudo = json.dumps(workflow, ensure_ascii=False)
    compose("exec", "-T", "n8n", "sh", "-c", "cat > /tmp/workflow.json", entrada=conteudo)
    saida = compose("exec", "-T", "n8n", "n8n", "import:workflow", "--input=/tmp/workflow.json")
    if saida.returncode != 0:
        sys.exit(f"Não consegui importar o workflow {workflow['id']} no n8n:\n{saida.stdout}{saida.stderr}")


def executar(workflow_id: str) -> str:
    saida = compose("exec", "-T", "n8n", "n8n", "execute", f"--id={workflow_id}")
    texto = saida.stdout + saida.stderr
    if "Execution was successful" in texto:
        return "verde"
    motivo = next((linha.strip() for linha in texto.splitlines() if "Conferência" in linha), "")
    return f"falhou ({motivo})" if motivo else "falhou"


def cenario(titulo: str, workflow: dict, execucoes: int, espera_entre_s: int = 0) -> None:
    preparar()
    importar(workflow)
    status = []
    for i in range(execucoes):
        if i and espera_entre_s:
            time.sleep(espera_entre_s)  # a mensagem não apagada volta a ficar visível
        status.append(executar(workflow["id"]))
    time.sleep(6)
    chamadas = chamadas_ao_reprocess()
    unicas = len({json.dumps(c["corpo"], sort_keys=True) for c in chamadas})
    print(f"\n{titulo}")
    print(f"  execuções:                    {', '.join(status)}")
    print(f"  chamadas ao /reprocess:       {len(chamadas)} ({unicas} mensagens diferentes)")
    print(f"  mensagens ainda na DLQ:       {mensagens_na_fila(DLQ)} de 5")
    print(f"  mensagens na quarentena:      {mensagens_na_fila(QUARENTENA)}")


def com_delete_errado(workflow: dict, workflow_id: str, manter_conferencia: bool) -> dict:
    """O erro clássico: depois do HTTP Request, $json é a resposta do /reprocess, não a mensagem."""
    variante = copy.deepcopy(workflow)
    variante["id"] = workflow_id
    for no in variante["nodes"]:
        if no["name"] == "Apagar da DLQ":
            no["parameters"]["body"] = (
                "={{ JSON.stringify({ QueueUrl: 'http://sqs:9324/000000000000/chat-dlq', "
                "ReceiptHandle: $json.ReceiptHandle }) }}"
            )
            no["onError"] = "continueRegularOutput"
    if not manter_conferencia:
        variante["nodes"] = [n for n in variante["nodes"] if n["name"] != "Conferência"]
        saidas = variante["connections"]["Receber da DLQ"]["main"][0]
        variante["connections"]["Receber da DLQ"]["main"][0] = [c for c in saidas if c["node"] != "Conferência"]
    return variante


def main() -> None:
    print(
        "Subindo n8n 2.44.5, SQS local e /reprocess falso (na primeira vez, o Docker baixa cerca de 330 MB)...",
        flush=True,
    )
    if subprocess.run(["docker", "compose", "up", "-d"], cwd=AQUI).returncode != 0:
        sys.exit(
            "O docker compose up falhou: confira se o Docker está rodando e se as portas 19324 e 18080 estão livres."
        )
    time.sleep(5)
    original = json.loads((AQUI / "workflows" / "original.json").read_text(encoding="utf-8"))
    corrigido = json.loads((AQUI / "workflows" / "corrigido.json").read_text(encoding="utf-8"))

    cenario("1. Fluxo como está, 3 execuções do agendamento", original, execucoes=3)
    cenario("2. Fluxo corrigido, 1 execução", corrigido, execucoes=1)
    cenario(
        "3. Corrigido, mas com o Delete lendo $json.ReceiptHandle, 'On Error: Continue' e sem a conferência",
        com_delete_errado(corrigido, "dlqVariante00001", manter_conferencia=False),
        execucoes=2,
        espera_entre_s=21,
    )
    cenario(
        "4. O mesmo erro do cenário 3, agora com o nó de conferência",
        com_delete_errado(corrigido, "dlqVariante00002", manter_conferencia=True),
        execucoes=1,
    )
    print("\nPara desligar: docker compose down (nesta pasta).")


if __name__ == "__main__":
    main()
