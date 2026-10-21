Caro recrutador,

Tratei os três problemas como se já fizesse parte do time que vai corrigir esses incidentes em produção: reproduzir, corrigir e medir. Além do que o enunciado pede, o repositório tem uma simulação que mostra o bug do exercício 1 no código original (`exercicio-1/simulacao.py`), o fluxo do exercício 2 rodando num n8n real (`exercicio-2/simulacao-n8n/`) e o prompt do exercício 3 testado contra modelos reais (`exercicio-3/evals/`).

## Como rodar

Precisa de Python 3.10 ou mais novo (testei do 3.10 ao 3.15) e, para o exercício 2, de Docker; no Mac e no Windows, o Docker Desktop. Só a criação do ambiente virtual muda de um sistema para outro:

```
python3 -m venv .venv && . .venv/bin/activate     # Linux e macOS
py -m venv .venv; .\.venv\Scripts\Activate.ps1    # Windows, no PowerShell
```

Daí em diante, os comandos são os mesmos nos três sistemas:

```bash
pip install -r requirements.txt
pytest                                           # 21 testes, sem rede e sem custo
python exercicio-1/simulacao.py                  # original x corrigido
python exercicio-2/simulacao-n8n/reproduzir.py   # precisa de Docker, leva uns 2 minutos
```

No Windows, se o `py` não existir, use `python`; se o PowerShell bloquear o `Activate.ps1`, rode antes `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`, que vale só para aquela janela. Com uv, sem criar o ambiente: `uv run --with-requirements requirements.txt pytest`. A cada push, o GitHub Actions roda os testes, a simulação do exercício 1 e o recálculo do eval no Linux, no macOS e no Windows. Nada acessa a AWS: as chamadas a Bedrock, DynamoDB e SQS são validadas com o Stubber do botocore. O eval do exercício 3 usa modelos reais e precisa de `ANTHROPIC_API_KEY`; os comandos estão no `casos_de_teste.md`.

## Exercício 1

Timeout intermitente e reprocessamento têm a mesma causa: o handler processa o lote em série, sem tratar erro por mensagem e sem olhar o tempo restante. No pico, a soma das chamadas ao modelo estoura o timeout e a Lambda devolve o lote inteiro, inclusive o que já foi respondido. A correção devolve só o que falhou (`batchItemFailures`), não começa chamada que não cabe no prazo, deixa os timeouts do SDK explícitos, injeta histórico e system prompt e registra uma linha de log JSON por mensagem. Streaming não ajuda aqui: com SQS no meio, a Lambda não tem conexão com o usuário.

## Exercício 2

Falha silenciosa é a execução do n8n terminar verde sem a mensagem sair da DLQ. Reproduzi num n8n 2.44.5 com SQS local: três execuções verdes, nada reprocessado, DLQ intacta. As causas vêm do receive e do delete montados à mão (o nó nativo de SQS do n8n só envia): o receive traz uma mensagem por padrão, o IF testa o Body sem converter de texto para JSON e descarta tudo, e o delete com receipt handle errado falha calado. A instrumentação mede o efeito: um nó de conferência faz a execução falhar quando recebidas e apagadas não batem, e alarmes olham se a DLQ esvazia. Mover mensagem com garantia de entrega eu deixaria com a AWS; o n8n fica com avisar e aprovar.

## Exercício 3

O prompt atual não diz o que fazer com "quero cancelar" sem alvo, e o modelo alterna entre perguntar e iniciar. O novo define quando o pedido está claro (o cliente nomeou o serviço ou confirmou a pergunta) e o que fazer nos casos vizinhos. Como o defeito é estatístico, o teste também é: 17 casos rodados dezenas de vezes contra os dois prompts, em dois modelos. O início indevido do cancelamento caiu de 27% e 40% para 0 em 60. Para detectar regressão em produção, priorizo a taxa de escalonamento para humano, lida por intenção e junto com abandono e recontato.

## Qual problema eu atacaria primeiro

O 1. Ele atinge todos os usuários no pico, e o reprocessamento aumenta a carga justamente quando ela já está alta. Também está por trás dos outros dois: alimenta a DLQ do problema 2, e o código atual nem envia o system prompt, então o ajuste do problema 3 depende dele para valer. O começo é configuração, coisa de horas: ligar `ReportBatchItemFailures`, ajustar timeout e tamanho do lote e trocar o modelo, que está em fim de vida no Bedrock. Eu inverteria a ordem só se o problema 3 estivesse efetivando cancelamentos sem confirmação, porque aí o dano ao cliente é direto e difícil de desfazer.

## Uso de IA

Usei o Claude Code (modelo Claude Opus 5.5) durante todo o teste: pesquisa na documentação oficial (AWS, n8n, Anthropic, legislação do SAC), rascunho de código, testes, simulações e textos, e execução dos testes, da reprodução no n8n e do eval. No exercício 3, os modelos testados (Claude Haiku 4.5 e Haiku 5.5) e o juiz (Claude Sonnet 5.5) são parte da solução; as chamadas custaram US$ 5,44.

O que eu mudei ou rejeitei: cortei a primeira versão da correção do exercício 1, que tinha processamento paralelo por sessão, backoff com `ChangeMessageVisibility`, fila de quarentena e controle de versão no histórico, por ser mais do que o enunciado pede. Cortei também arquivos de infraestrutura (template SAM e alarmes em CloudFormation) e partes do eval usadas uma vez. E pedi para reproduzir o exercício 2 num n8n real em vez de só descrever, o que revelou uma causa que a análise não previa (o formato de resposta "JSON" do HTTP Request não interpreta a resposta do SQS).

Na verificação caíram: a primeira rubrica do juiz, que tratava transferência para humano como obstáculo (corrigida, com as mesmas respostas rejulgadas); a recomendação genérica de `read_timeout` de 3600 s para o Bedrock, inadequada para chat; e a citação só do Decreto 11.034, revogado pelo 13.135 em outubro de 2026.

## O que não foi verificado

Nada foi implantado na AWS. A reprodução do exercício 2 usa um SQS local, que difere da AWS no delete com receipt handle vencido, e eu a rodei só no Linux: no macOS e no Windows, o CI cobre os testes e a simulação, mas não o Docker. O eval rodou na API da Anthropic, e não no Bedrock, e o juiz não foi calibrado contra avaliação humana.
