"""Ambiente falso para importar o handler: o módulo cria os clientes AWS na carga, como
em produção, mas nenhum teste fala com a AWS."""

import os
import tempfile
from pathlib import Path

_prompt = Path(tempfile.mkdtemp()) / "system_prompt.txt"
_prompt.write_text("Você é o assistente virtual de suporte. (prompt de teste)", encoding="utf-8")

os.environ.update(
    {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "teste",
        "AWS_SECRET_ACCESS_KEY": "teste",
        "MODEL_ID": "global.anthropic.claude-haiku-5-5",
        "SESSION_TABLE": "chat-sessoes",
        "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/chat-respostas",
        "SYSTEM_PROMPT_PATH": str(_prompt),
    }
)
