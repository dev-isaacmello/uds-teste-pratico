# Exercício 1: diagnóstico e correção

Timeout intermitente e reprocessamento desnecessário têm a mesma causa: o handler processa o lote em série, sem tratar erro por mensagem e sem olhar o tempo restante. No pico, a soma das chamadas ao modelo passa do timeout e a Lambda devolve o lote inteiro, inclusive o que já foi respondido; um erro em qualquer mensagem faz o mesmo. O que esgota as tentativas cai na DLQ do problema 2.

Reproduzi isso com dublês em `simulacao.py` (lote de 10, timeout de 60 s). No pico, com 8 s por resposta, o original estoura o timeout e descarta 7 respostas já geradas; o corrigido responde 3 e devolve 7 sem chamar o modelo. Com throttling numa mensagem, o original devolve as 10 e o corrigido só a que falhou. Com uma mensagem malformada, depois de 5 tentativas o original leva 9 válidas para a DLQ; o corrigido responde as 9.

## a) Problemas

1. Sem tratamento de erro por mensagem (tratamento de erros). Qualquer exceção devolve o lote inteiro: resposta duplicada, custo dobrado e mensagens válidas arrastadas para a DLQ por uma malformada.
2. Lote em série sem controle de prazo (resiliência). No pico as latências se somam e o lote estoura o timeout. É intermitente porque fora do pico o lote é pequeno e cabe.
3. Timeouts e retries implícitos (resiliência). O boto3 usa por padrão o modo legacy, até 5 tentativas e 60 s de leitura: uma chamada lenta segura o lote até o timeout.
4. Sem contexto (contexto). O `session_id` é ignorado e o system prompt não é enviado: o modelo responde cada mensagem como se fosse a primeira e o tom fica por conta dele, o que se liga ao problema 3.
5. `print` do resultado inteiro (observabilidade). Joga dado pessoal no CloudWatch e não registra id, latência, tokens nem tipo de erro; não dá para saber se os 12 s vêm da fila, do modelo ou de retry.
6. A resposta não é entregue nem salva. O cliente SQS e o `QUEUE_URL` existem e nunca são usados.
7. Modelo fixo e em fim de vida. O Claude 3 Sonnet está como Legacy no Bedrock, com fim de vida em 2026 ([lifecycle](https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle-legacy.html)). E o `max_tokens` de 2048 reserva cota à toa, porque o Bedrock desconta esse valor da cota de tokens por minuto no início de cada chamada.
8. Clientes boto3 criados a cada invocação, sem reaproveitar conexões.

## b) Correção

O `codigo_corrigido.py` mantém o processamento em série e muda o necessário:

- Falha parcial de lote: `try/except` por mensagem e `batchItemFailures` só com as que falharam. Em fila FIFO, para na primeira falha e devolve o resto, como a AWS orienta.
- Prazo: antes de chamar o modelo, confere se o pior caso da chamada (duas tentativas de 2 s + 15 s, mais margem, 37 s) cabe no tempo restante. Se não cabe, devolve a mensagem sem chamar.
- Clientes criados uma vez, fora do handler, com timeout e número de tentativas explícitos.
- Contexto: lê os últimos 10 turnos da sessão no DynamoDB, monta a conversa na API Converse e envia o system prompt separado da mensagem do cliente. Depois grava o turno e publica a resposta na fila de respostas.
- Reentrega (o SQS entrega pelo menos uma vez): se a mensagem já foi respondida, reenvia a resposta gravada sem chamar o modelo.
- Observabilidade: uma linha de log JSON por mensagem, com ids, status, tempo na fila, latência do modelo e tokens, sem o texto da conversa.
- Modelo por variável de ambiente. Em produção eu usaria o perfil global do Claude Haiku 5.5, que distribui carga entre regiões e foi o melhor no eval do exercício 3, com `max_tokens` de 600.

Premissas: a mensagem tem `session_id`, `message` e, opcionalmente, `message_id`; `QUEUE_URL` é a fila de respostas; o system prompt vai como arquivo junto da função, porque as variáveis de ambiente da Lambda somam no máximo 4 KB.

Parte da correção é configuração do gatilho:

```yaml
Events:
  Entrada:
    Type: SQS
    Properties:
      Queue: !GetAtt FilaDeEntrada.Arn
      BatchSize: 3
      MaximumBatchingWindowInSeconds: 0
      FunctionResponseTypes: [ReportBatchItemFailures]
      ScalingConfig:
        MaximumConcurrency: 50
```

Sem `ReportBatchItemFailures`, o `batchItemFailures` é ignorado. O `BatchSize` de 3 sai da conta do prazo (com 8 s por resposta e 37 s reservados por chamada, cabem 3 em 60 s) e faz a Lambda processar lotes em paralelo em vez de somar latências. O `MaximumConcurrency` mantém as chamadas ao Bedrock dentro da cota. Na fila, visibility timeout de 6 vezes o timeout da função e DLQ com `maxReceiveCount` 5, como a AWS recomenda.

Os 8 testes (`pytest exercicio-1`) cobrem falha parcial, mensagem malformada, FIFO, prazo, contexto, reentrega e log. Um deles valida as chamadas a Bedrock, DynamoDB e SQS contra o modelo real das APIs com o Stubber, sem acessar a AWS.

## c) `invoke_model` x `invoke_model_with_response_stream`

Com SQS na entrada, streaming não reduz a latência percebida: a Lambda não tem conexão com o usuário e publicaria a resposta inteira no fim do mesmo jeito. Para o usuário ver a resposta sendo escrita, é preciso um canal de push, como API Gateway WebSocket. Aí entram os custos: um erro no meio do stream chega depois de o cliente ver texto parcial, e retentar pela fila duplica texto; guardrails seguram pedaços para avaliar; cada pedaço vira uma chamada ao canal; e o tempo de Lambda é o mesmo. O tempo total de geração não muda, só o primeiro pedaço chega antes.

Eu manteria o síncrono (`converse`). A maior parte dos 12 s está antes do modelo: lote em série, retries e reprocessamento. Com o log separando tempo na fila e latência do modelo, a decisão de levar o chat para WebSocket com `converse_stream` passa a ser tomada com dado.

Não verificado: nada foi implantado na AWS, e os 8 s do pico são da simulação. Se a função rodar em VPC com NAT, conexão ociosa derrubada depois de 350 s é outra fonte de pico intermitente a conferir.
