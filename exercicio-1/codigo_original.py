# Código original do enunciado, mantido sem alterações para referência e para a
# simulação (simulacao.py) reproduzir os sintomas descritos.
import boto3, json, os

def handler(event, context):
    bedrock = boto3.client('bedrock-runtime')
    sqs = boto3.client('sqs')
    queue_url = os.environ['QUEUE_URL']

    for record in event['Records']:
        body = json.loads(record['body'])
        user_input = body['message']
        session_id = body['session_id']

        response = bedrock.invoke_model(
            modelId='anthropic.claude-3-sonnet-20240229-v1:0',
            body=json.dumps({
                'anthropic_version': 'bedrock-2023-05-31',
                'max_tokens': 2048,
                'messages': [{'role': 'user', 'content': user_input}]
            }),
            contentType='application/json',
            accept='application/json'
        )

        result = json.loads(response['body'].read())
        print(f'Resposta: {result}')

    return {'statusCode': 200}
