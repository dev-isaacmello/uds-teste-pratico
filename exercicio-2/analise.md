# Exercício 2: falha silenciosa no reprocessamento da DLQ

Falha silenciosa aqui é a execução do n8n terminar verde sem a mensagem sair da DLQ. Para não ficar na hipótese, montei o fluxo num n8n 2.44.5 com SQS local (ElasticMQ) e um `/reprocess` falso, em `simulacao-n8n/` (roda com `reproduzir.py` e Docker). Com 5 mensagens na DLQ, o fluxo como costuma ser montado teve três execuções verdes, nenhuma chamada ao `/reprocess` e as 5 mensagens continuaram lá. O corrigido reprocessou as 4 válidas, mandou a malformada para quarentena e esvaziou a fila.

O ponto de partida: o nó nativo de SQS do n8n só envia mensagem ([documentação](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.awssqs/)). Receive e delete viram HTTP Requests montados à mão, e daí vem a maior parte das causas.

## a) Causas

Reproduzidas:

- Uma mensagem por execução. O `ReceiveMessage` tem `MaxNumberOfMessages` padrão 1 e, sem `WaitTimeSeconds`, faz short polling, que pode voltar vazio ([API](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ReceiveMessage.html)). Com agendamento de 5 minutos, o fluxo drena 288 mensagens por dia; se entrar mais, acumula sem erro.
- O IF descarta tudo. O `Body` chega como texto, então `{{ $json.Body.session_id }}` sem converter para JSON dá falso. O ramo falso não leva a lugar nenhum, a execução fica verde e a mensagem volta a ficar visível no fim do visibility timeout.
- Delete com receipt handle errado. Depois do HTTP Request, `$json` é a resposta do `/reprocess`, não a mensagem. O delete falha e, com "On Error: Continue", ninguém vê: na reprodução, foram 8 chamadas ao `/reprocess` para 4 mensagens, tudo verde. O certo é o item pareado, `{{ $('Ler corpo').item.json.ReceiptHandle }}`.
- Formato de resposta. Com "Response Format: JSON" forçado, o n8n 2.44.5 não interpreta a resposta do SQS (`application/x-amz-json-1.0`) e o Split Out termina sem itens e sem erro. Com Autodetect, funciona.

Documentadas, sem reprodução:

- Visibility timeout menor que o fluxo, com execuções sobrepostas (o n8n não impede por padrão). O delete com o receipt handle antigo "will succeed, but the message might not be deleted" ([API](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_DeleteMessage.html)). O ElasticMQ responde erro nesse caso, por isso não reproduzi. O inverso, visibility timeout longo num fluxo que não apaga, deixa mensagens presas em voo.
- IAM. Sem `sqs:DeleteMessage`, o delete falha calado com "On Error: Continue". Com KMS, falta `kms:Decrypt` na chave da DLQ ou na da fila de origem. E uma credencial apontando para outra fila, conta ou região funciona perfeitamente contra uma fila vazia.
- Idempotência do `/reprocess`. Sem ela, acontece a duplicação acima. E se o `/reprocess` só reenfileira (202) e o consumer falha de novo pela mesma causa, a mensagem volta para a DLQ e o saldo nunca cai.

## b) Instrumentação sem mudar a lógica principal

A ideia é medir o efeito, não a cor da execução.

- Nó de conferência num ramo separado: compara recebidas com apagadas e faz a execução falhar quando não batem. Na reprodução, o erro do delete que passava verde passou a falhar com `{"recebidas":5,"reprocessadas":4,"quarentena":1,"apagadas":1}`. Isso dá ao Error Workflow (Error Trigger avisando no Slack ou por e-mail) algo para pegar, porque ele só dispara em execução com erro.
- Ramo falso do IF ligado a uma fila de quarentena, execuções salvas para auditoria e um heartbeat, uma métrica publicada a cada execução.
- Na AWS, sem tocar no n8n, três alarmes do CloudWatch na DLQ: há leitura e o número de mensagens não cai por algumas horas; há mensagens recebidas e nenhuma apagada; o heartbeat sumiu. Evito o alarme pela idade da mensagem mais antiga, porque a AWS exclui dele justamente as mensagens recebidas e nunca apagadas ([métricas do SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html)). Os data events do CloudTrail mostram cada `DeleteMessage` negado.
- O workflow corrigido manda o `MessageId` no cabeçalho `Idempotency-Key` do `/reprocess`.

## c) Quando trocar por solução nativa da AWS

O fluxo mistura duas coisas: mover mensagem com garantia de entrega, onde o n8n é frágil, e decidir e avisar, onde ele é bom. Eu trocaria quando:

- há dado pessoal e o n8n é Cloud (LGPD);
- o volume é contínuo ou o reprocessamento precisa reagir em segundos;
- a credencial é uma chave de acesso guardada no n8n, e não uma role IAM;
- a lógica é técnica e estável.

Manteria no n8n com volume baixo, sem dado sensível, self-hosted e com uma regra que a operação muda toda semana.

As opções nativas:

- Redrive do próprio SQS (`StartMessageMoveTask`): devolve a DLQ para a fila de origem com controle de velocidade, mas não filtra mensagens.
- Uma Lambda lendo a DLQ: resolve quando o reprocessamento tem regra (limite de tentativas, mensagem velha) e já apaga o que deu certo.
- EventBridge Pipes: serve para roteamento simples, mas o filtro apaga da fila o que não passa nele, e o destino HTTP tem limite de 5 s.

Neste caso eu trocaria: são conversas de clientes, e as falhas estão justamente na parte que a AWS faz sozinha. A DLQ guardaria só falha transitória, o redrive nativo devolve tudo depois que a causa for corrigida, e o n8n fica com avisar o time e oferecer o botão do redrive. Como é chat, uma mensagem velha demais não deve ser respondida automaticamente horas depois; o caminho é abrir atendimento humano.
