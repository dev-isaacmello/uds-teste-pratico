"""Configuração do chatbot avaliado: prompts, ferramentas e montagem da requisição.

As duas versões de prompt recebem exatamente o mesmo contexto do cliente e as mesmas
ferramentas; a única variável do experimento é o texto do system prompt.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PROMPT_ATUAL = (
    "Você é um assistente virtual de suporte ao cliente. Responda de forma objetiva e cordial. "
    "Não forneça informações incorretas."
)

ARQUIVO_PROMPT_NOVO = Path(__file__).resolve().parent.parent / "system_prompt.md"

# Mesmas definições que o backend registraria no Converse/Messages. O fluxo de
# cancelamento em si (confirmação final, protocolo, efeito imediato) é do backend.
FERRAMENTAS: list[dict[str, Any]] = [
    {
        "name": "iniciar_cancelamento",
        "description": (
            "Inicia o fluxo de cancelamento de um serviço ativo do cliente. O fluxo mostra ao cliente "
            "o resumo do cancelamento, registra o pedido e gera o protocolo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "servico_id": {
                    "type": "string",
                    "description": "id exato de um item de servicos_ativos no contexto do cliente",
                }
            },
            "required": ["servico_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "transferir_para_atendente",
        "description": "Transfere a conversa para um atendente humano, com um resumo do caso.",
        "input_schema": {
            "type": "object",
            "properties": {"motivo": {"type": "string", "description": "resumo curto do que o cliente precisa"}},
            "required": ["motivo"],
            "additionalProperties": False,
        },
    },
]


def carregar_prompt(nome: str) -> str:
    if nome == "atual":
        return PROMPT_ATUAL
    if nome == "novo":
        return extrair_prompt(ARQUIVO_PROMPT_NOVO.read_text(encoding="utf-8"))
    raise ValueError(f"prompt desconhecido: {nome}")


def extrair_prompt(markdown: str) -> str:
    """O system_prompt.md é a fonte única: o prompt fica entre marcadores, num bloco de código."""
    trecho = re.search(r"<!-- prompt:inicio -->\s*```[a-z]*\n(.*?)```\s*<!-- prompt:fim -->", markdown, re.S)
    if trecho is None:
        raise ValueError("system_prompt.md sem os marcadores <!-- prompt:inicio --> e <!-- prompt:fim -->")
    return trecho.group(1).strip()


def montar_system(prompt: str, contexto_cliente: str) -> str:
    return f"{prompt}\n\n<contexto_do_cliente>\n{contexto_cliente.strip()}\n</contexto_do_cliente>"


def montar_mensagens(conversa: list[dict[str, str]]) -> list[dict[str, str]]:
    papeis = {"cliente": "user", "assistente": "assistant"}
    return [{"role": papeis[turno["papel"]], "content": turno["texto"]} for turno in conversa]
