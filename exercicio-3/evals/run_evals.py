"""Roda a suíte de comportamento (casos.yaml) contra o prompt atual e o novo e grava o resultado.

    python exercicio-3/evals/run_evals.py                                      # suíte completa, cerca de US$ 3
    python exercicio-3/evals/run_evals.py --casos C01 C02 --reps 3 --reps-ancora 3
    python exercicio-3/evals/run_evals.py --resumir exercicio-3/evals/resultados/<rodada>   # sem custo

Precisa de ANTHROPIC_API_KEY. Cada rodada fica em resultados/<data-hora>/: results.jsonl (uma
linha por resposta, com o texto, as ferramentas chamadas e a nota), errors.jsonl (falhas de
API, que ficam fora da nota) e resumo.md.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import anthropic
import yaml
from avaliacao import (
    INICIO_INDEVIDO,
    JUIZ_SYSTEM,
    Julgamento,
    Observacao,
    avaliar,
    fisher_bilateral,
    ids_internos,
    usa_markdown,
    wilson,
)
from chatbot import FERRAMENTAS, carregar_prompt, montar_mensagens, montar_system

AQUI = Path(__file__).resolve().parent
# US$ por milhão de tokens (entrada, saída) na API da Anthropic.
PRECOS = {"claude-haiku-4-5": (1.00, 5.00), "claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}
# Configuração de produção de cada modelo testado: chat com esforço baixo de raciocínio.
PERFIL = {"claude-haiku-4-5": {}, "claude-haiku-5-5": {"output_config": {"effort": "low"}}}
JUIZ = "claude-sonnet-5-5"
_lock = threading.Lock()


def custo_usd(modelo: str, usage: Any) -> float:
    entrada, saida = PRECOS[modelo]
    return (usage.input_tokens * entrada + usage.output_tokens * saida) / 1e6


def chamar_chatbot(client: anthropic.Anthropic, modelo: str, prompt: str, caso: dict, contexto: str):
    inicio = time.monotonic()
    resp = client.messages.create(
        model=modelo,
        max_tokens=4096,
        system=montar_system(prompt, contexto),
        messages=montar_mensagens(caso["conversa"]),
        tools=FERRAMENTAS,
        **PERFIL[modelo],
    )
    if not resp.model.startswith(modelo):
        raise RuntimeError(f"pedi {modelo}, respondeu {resp.model}")
    obs = Observacao(
        texto="".join(b.text for b in resp.content if b.type == "text").strip(),
        ferramentas=[{"name": b.name, "input": b.input} for b in resp.content if b.type == "tool_use"],
        stop_reason=resp.stop_reason,
    )
    return obs, resp, time.monotonic() - inicio


def julgar(client: anthropic.Anthropic, caso: dict, contexto: str, obs: Observacao):
    conversa = "\n".join(f"{t['papel']}: {t['texto']}" for t in caso["conversa"])
    chamadas = "; ".join(f"{f['name']}({json.dumps(f['input'], ensure_ascii=False)})" for f in obs.ferramentas)
    conteudo = (
        f"<contexto_do_cliente>\n{contexto.strip()}\n</contexto_do_cliente>\n\n"
        f"<conversa>\n{conversa}\n</conversa>\n\n"
        f"<resposta_do_chatbot>\n{obs.texto or '(sem texto)'}\n</resposta_do_chatbot>\n\n"
        f"<ferramentas_chamadas>{chamadas or 'nenhuma'}</ferramentas_chamadas>"
    )
    resp = client.messages.parse(
        model=JUIZ,
        max_tokens=4096,
        system=JUIZ_SYSTEM,
        messages=[{"role": "user", "content": conteudo}],
        output_format=Julgamento,
        output_config={"effort": "medium"},
    )
    return resp.parsed_output, resp


def gravar(arquivo: Path, linha: dict) -> None:
    with _lock, arquivo.open("a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")


def executar(tarefa: tuple, client: anthropic.Anthropic, contextos: dict, prompts: dict, pasta: Path) -> None:
    nome_prompt, modelo, caso, rep = tarefa
    chave = {"prompt": nome_prompt, "modelo": modelo, "caso": caso["id"], "rep": rep}
    contexto = contextos[caso["contexto"]]
    try:
        obs, resp, latencia_s = chamar_chatbot(client, modelo, prompts[nome_prompt], caso, contexto)
        julgamento, resp_juiz = julgar(client, caso, contexto, obs) if obs.texto else (None, None)
    except (anthropic.APIError, RuntimeError) as exc:
        gravar(pasta / "errors.jsonl", {**chave, "erro": type(exc).__name__, "detalhe": str(exc)[:500]})
        return
    nota = avaliar(caso, obs, julgamento, ids_internos(contexto))
    gravar(
        pasta / "results.jsonl",
        {
            **chave,
            "ancora": bool(caso.get("ancora")),
            "classe": nota.classe,
            "passou": nota.passou,
            "motivos": nota.motivos,
            "inicio_indevido": nota.classe in INICIO_INDEVIDO and nota.classe not in caso["aceitas"],
            "pode_iniciar": bool(INICIO_INDEVIDO & set(caso["aceitas"])),
            "markdown": usa_markdown(obs.texto),
            "texto": obs.texto,
            "ferramentas": obs.ferramentas,
            "julgamento": julgamento.model_dump() if julgamento else None,
            "latencia_s": round(latencia_s, 3),
            "custo_usd": round(custo_usd(modelo, resp.usage), 6),
            "custo_juiz_usd": round(custo_usd(JUIZ, resp_juiz.usage), 6) if resp_juiz else 0.0,
        },
    )


def resumir(linhas: list[dict], casos: list[dict], erros: list[dict]) -> str:
    variantes = sorted({(r["prompt"], r["modelo"]) for r in linhas}, key=lambda v: (v[1], v[0]))
    out = [
        "# Resultado da suíte de comportamento",
        "",
        f"Juiz: `{JUIZ}`. Respostas avaliadas: {len(linhas)}. Falhas de API: {len(erros)}.",
        "",
        "| prompt | modelo | execuções | taxa de acerto (IC 95%) | início indevido nas âncoras "
        "| início indevido onde não podia | com markdown | latência p50 / p95 | custo do chatbot |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for prompt, modelo in variantes:
        rs = [r for r in linhas if (r["prompt"], r["modelo"]) == (prompt, modelo)]
        ok = sum(r["passou"] for r in rs)
        baixo, alto = wilson(ok, len(rs))
        ancoras = [r for r in rs if r["ancora"]]
        proibidos = [r for r in rs if not r["pode_iniciar"]]
        latencias = sorted(r["latencia_s"] for r in rs)
        out.append(
            f"| {prompt} | {modelo} | {len(rs)} | {ok / len(rs):.0%} ({baixo:.0%} a {alto:.0%}) "
            f"| {sum(r['inicio_indevido'] for r in ancoras)}/{len(ancoras)} "
            f"| {sum(r['inicio_indevido'] for r in proibidos)}/{len(proibidos)} "
            f"| {sum(r['markdown'] for r in rs) / len(rs):.0%} "
            f"| {latencias[len(latencias) // 2]:.1f} s / {latencias[int(len(latencias) * 0.95)]:.1f} s "
            f"| US$ {sum(r['custo_usd'] for r in rs):.3f} |"
        )

    out += ["", "Início indevido nas âncoras, prompt atual x novo (teste exato de Fisher, bilateral):", ""]
    for modelo in sorted({m for _, m in variantes}):
        contagem = {}
        for prompt in ("atual", "novo"):
            ancoras = [r for r in linhas if (r["prompt"], r["modelo"], r["ancora"]) == (prompt, modelo, True)]
            contagem[prompt] = (sum(r["inicio_indevido"] for r in ancoras), len(ancoras))
        if all(n for _, n in contagem.values()):
            (a, na), (b, nb) = contagem["atual"], contagem["novo"]
            out.append(f"- `{modelo}`: atual {a}/{na}, novo {b}/{nb}, p = {fisher_bilateral(a, na - a, b, nb - b):.2g}")

    out += ["", "| caso | " + " | ".join(f"{p} · {m}" for p, m in variantes) + " |"]
    out.append("|---|" + "---|" * len(variantes))
    for caso in casos:
        celulas = []
        for prompt, modelo in variantes:
            rs = [r for r in linhas if (r["prompt"], r["modelo"], r["caso"]) == (prompt, modelo, caso["id"])]
            celulas.append(f"{sum(r['passou'] for r in rs)}/{len(rs)}" if rs else "sem dados")
        out.append(f"| {caso['id']} {caso['nome']} | " + " | ".join(celulas) + " |")

    out += ["", "Motivos de reprovação mais frequentes:", ""]
    for prompt, modelo in variantes:
        motivos = Counter(
            f"{r['caso']}: {m}" for r in linhas if (r["prompt"], r["modelo"]) == (prompt, modelo) for m in r["motivos"]
        )
        out.append(
            f"- {prompt} · {modelo}: " + ("; ".join(f"{m} ({n}x)" for m, n in motivos.most_common(5)) or "nenhum")
        )
    custo = sum(r["custo_usd"] + r["custo_juiz_usd"] for r in linhas)
    out += ["", f"Custo das chamadas desta rodada: US$ {custo:.2f}."]
    return "\n".join(out) + "\n"


def ler_jsonl(arquivo: Path) -> list[dict]:
    return [json.loads(linha) for linha in arquivo.read_text(encoding="utf-8").splitlines()] if arquivo.exists() else []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompts", nargs="+", default=["atual", "novo"])
    parser.add_argument("--modelos", nargs="+", default=["claude-haiku-4-5", "claude-haiku-5-5"])
    parser.add_argument("--casos", nargs="*", help="ids dos casos (padrão: todos)")
    parser.add_argument("--reps", type=int, default=10, help="execuções por caso")
    parser.add_argument("--reps-ancora", type=int, default=30, help="execuções por caso âncora")
    parser.add_argument("--resumir", type=Path, help="refaz o resumo.md de uma rodada salva, sem chamar modelos")
    args = parser.parse_args()

    suite = yaml.safe_load((AQUI / "casos.yaml").read_text(encoding="utf-8"))
    casos = [c for c in suite["casos"] if not args.casos or c["id"] in args.casos]

    if args.resumir:
        pasta = args.resumir
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            parser.error("defina ANTHROPIC_API_KEY para chamar os modelos (o --resumir não precisa)")
        pasta = AQUI / "resultados" / datetime.now().strftime("%Y%m%d-%H%M%S")
        pasta.mkdir(parents=True)
        prompts = {nome: carregar_prompt(nome) for nome in args.prompts}
        config = {"modelos": args.modelos, "juiz": JUIZ, "perfil": PERFIL, "prompts": prompts}
        (pasta / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        tarefas = [
            (p, m, caso, rep)
            for p in args.prompts
            for m in args.modelos
            for caso in casos
            for rep in range(args.reps_ancora if caso.get("ancora") else args.reps)
        ]
        client = anthropic.Anthropic(max_retries=4)
        print(f"{len(tarefas)} execuções em {pasta}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda t: executar(t, client, suite["contextos"], prompts, pasta), tarefas))

    resumo = resumir(ler_jsonl(pasta / "results.jsonl"), casos, ler_jsonl(pasta / "errors.jsonl"))
    (pasta / "resumo.md").write_text(resumo, encoding="utf-8")
    print(resumo)


if __name__ == "__main__":
    main()
