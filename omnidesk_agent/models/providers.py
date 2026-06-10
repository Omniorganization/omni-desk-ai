from __future__ import annotations
import base64, json, os
from dataclasses import dataclass
from typing import Any
import httpx
from omnidesk_agent.models.base import ModelRequest, ModelResponse

@dataclass
class ProviderSettings:
    profile_name: str; provider: str; model: str
    api_key_env: str | None = None; base_url: str | None = None; api_version: str | None = None; region: str | None = None
    max_output_tokens: int = 1200; temperature: float = 0.2
    extra_headers: dict[str,str] | None = None; extra_body: dict[str,Any] | None = None

def env(name: str | None) -> str: return os.getenv(name or '', '')
def msgs(system,user): return [{'role':'system','content':system},{'role':'user','content':user}]

class OpenAIResponsesProvider:
    provider_name='openai'
    def __init__(self,s:ProviderSettings): self.settings=s; self.model=s.model; self.profile_name=s.profile_name
    async def complete(self, request:ModelRequest)->ModelResponse:
        key=env(self.settings.api_key_env); 
        if not key: raise RuntimeError(f'Missing {self.settings.api_key_env}')
        base=(self.settings.base_url or 'https://api.openai.com/v1').rstrip('/')
        body={'model':self.model,'input':[{'role':'system','content':request.system},{'role':'user','content':request.user}], 'max_output_tokens':self.settings.max_output_tokens}
        if request.json_mode: body['text']={'format':{'type':'json_object'}}
        async with httpx.AsyncClient(timeout=120) as c:
            rr=await c.post(f'{base}/responses', headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'}, json=body); rr.raise_for_status(); data=rr.json()
        text=data.get('output_text') or ''
        if not text:
            text=''.join(part.get('text','') for item in data.get('output',[]) for part in item.get('content',[]) if part.get('type') in {'output_text','text'})
        return ModelResponse(text=text, provider=self.provider_name, model=self.model, profile=self.profile_name, usage=data.get('usage'), raw=data)

class OpenAICompatibleProvider:
    provider_name='openai_compatible'
    def __init__(self,s:ProviderSettings): self.settings=s; self.model=s.model; self.profile_name=s.profile_name
    async def complete(self, request:ModelRequest)->ModelResponse:
        key=env(self.settings.api_key_env); 
        if not key: raise RuntimeError(f'Missing {self.settings.api_key_env}')
        base=(self.settings.base_url or 'https://api.openai.com/v1').rstrip('/')
        body={'model':self.model,'messages':msgs(request.system,request.user),'temperature':self.settings.temperature,'max_tokens':self.settings.max_output_tokens}
        if request.json_mode: body['response_format']={'type':'json_object'}
        if self.settings.extra_body: body.update(self.settings.extra_body)
        headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'}
        if self.settings.extra_headers: headers.update(self.settings.extra_headers)
        async with httpx.AsyncClient(timeout=120) as c:
            rr=await c.post(f'{base}/chat/completions', headers=headers, json=body); rr.raise_for_status(); data=rr.json()
        return ModelResponse(text=data.get('choices',[{}])[0].get('message',{}).get('content','') or '', provider=self.provider_name, model=self.model, profile=self.profile_name, usage=data.get('usage'), raw=data)

class AzureOpenAIProvider(OpenAICompatibleProvider):
    provider_name='azure_openai'
    async def complete(self, request):
        key=env(self.settings.api_key_env); endpoint=(self.settings.base_url or '').rstrip('/')
        if not key: raise RuntimeError(f'Missing {self.settings.api_key_env}')
        if not endpoint: raise RuntimeError('Azure OpenAI base_url is required')
        ver=self.settings.api_version or '2024-10-21'
        body={'messages':msgs(request.system,request.user),'temperature':self.settings.temperature,'max_tokens':self.settings.max_output_tokens}
        async with httpx.AsyncClient(timeout=120) as c:
            rr=await c.post(f'{endpoint}/openai/deployments/{self.model}/chat/completions?api-version={ver}', headers={'api-key':key,'Content-Type':'application/json'}, json=body); rr.raise_for_status(); data=rr.json()
        return ModelResponse(text=data.get('choices',[{}])[0].get('message',{}).get('content','') or '', provider=self.provider_name, model=self.model, profile=self.profile_name, usage=data.get('usage'), raw=data)

class AnthropicProvider:
    provider_name='anthropic'
    def __init__(self,s): self.settings=s; self.model=s.model; self.profile_name=s.profile_name
    async def complete(self, request):
        key=env(self.settings.api_key_env); 
        if not key: raise RuntimeError(f'Missing {self.settings.api_key_env}')
        base=(self.settings.base_url or 'https://api.anthropic.com').rstrip('/')
        body={'model':self.model,'max_tokens':self.settings.max_output_tokens,'temperature':self.settings.temperature,'system':request.system,'messages':[{'role':'user','content':request.user}]}
        async with httpx.AsyncClient(timeout=120) as c:
            rr=await c.post(f'{base}/v1/messages', headers={'x-api-key':key,'anthropic-version':self.settings.api_version or '2023-06-01','content-type':'application/json'}, json=body); rr.raise_for_status(); data=rr.json()
        text=''.join(x.get('text','') for x in data.get('content',[]) if x.get('type')=='text')
        return ModelResponse(text=text, provider=self.provider_name, model=self.model, profile=self.profile_name, usage=data.get('usage'), raw=data)

class GeminiProvider:
    provider_name='gemini'
    def __init__(self,s): self.settings=s; self.model=s.model; self.profile_name=s.profile_name
    async def complete(self, request):
        key=env(self.settings.api_key_env); 
        if not key: raise RuntimeError(f'Missing {self.settings.api_key_env}')
        parts=[{'text':f'{request.system}\n\n{request.user}'}]
        for img in request.images:
            with open(img,'rb') as f: parts.append({'inline_data':{'mime_type':'image/png','data':base64.b64encode(f.read()).decode('ascii')}})
        body={'contents':[{'role':'user','parts':parts}], 'generationConfig':{'temperature':self.settings.temperature,'maxOutputTokens':self.settings.max_output_tokens}}
        base=(self.settings.base_url or 'https://generativelanguage.googleapis.com').rstrip('/')
        async with httpx.AsyncClient(timeout=120) as c:
            rr=await c.post(f'{base}/v1beta/models/{self.model}:generateContent', params={'key':key}, json=body); rr.raise_for_status(); data=rr.json()
        text=''.join(p.get('text','') for cand in data.get('candidates',[]) for p in cand.get('content',{}).get('parts',[]))
        return ModelResponse(text=text, provider=self.provider_name, model=self.model, profile=self.profile_name, usage=data.get('usageMetadata'), raw=data)

class OllamaProvider:
    provider_name='ollama'
    def __init__(self,s): self.settings=s; self.model=s.model; self.profile_name=s.profile_name
    async def complete(self, request):
        base=(self.settings.base_url or 'http://127.0.0.1:11434').rstrip('/')
        body={'model':self.model,'stream':False,'messages':msgs(request.system,request.user),'options':{'temperature':self.settings.temperature,'num_predict':self.settings.max_output_tokens}}
        async with httpx.AsyncClient(timeout=180) as c:
            rr=await c.post(f'{base}/api/chat', json=body); rr.raise_for_status(); data=rr.json()
        return ModelResponse(text=data.get('message',{}).get('content',''), provider=self.provider_name, model=self.model, profile=self.profile_name, raw=data)

class CohereProvider(OpenAICompatibleProvider):
    provider_name='cohere'
    async def complete(self, request):
        key=env(self.settings.api_key_env); 
        if not key: raise RuntimeError(f'Missing {self.settings.api_key_env}')
        base=(self.settings.base_url or 'https://api.cohere.com').rstrip('/')
        body={'model':self.model,'messages':msgs(request.system,request.user),'temperature':self.settings.temperature,'max_tokens':self.settings.max_output_tokens}
        async with httpx.AsyncClient(timeout=120) as c:
            rr=await c.post(f'{base}/v2/chat', headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'}, json=body); rr.raise_for_status(); data=rr.json()
        content=data.get('message',{}).get('content','')
        if isinstance(content,list): content=''.join(x.get('text','') for x in content if isinstance(x,dict))
        return ModelResponse(text=str(content or ''), provider=self.provider_name, model=self.model, profile=self.profile_name, usage=data.get('usage'), raw=data)

class BaiduQianfanProvider(OpenAICompatibleProvider):
    provider_name='baidu_qianfan'

class BedrockProvider:
    provider_name='bedrock'
    def __init__(self,s): self.settings=s; self.model=s.model; self.profile_name=s.profile_name
    async def complete(self, request):
        try: import boto3
        except ImportError as exc: raise RuntimeError('Install boto3 to use AWS Bedrock provider') from exc
        client=boto3.client('bedrock-runtime', region_name=self.settings.region or os.getenv('AWS_REGION','us-east-1'))
        body=json.dumps({'anthropic_version':'bedrock-2023-05-31','max_tokens':self.settings.max_output_tokens,'temperature':self.settings.temperature,'system':request.system,'messages':[{'role':'user','content':request.user}]})
        resp=client.invoke_model(modelId=self.model, body=body, contentType='application/json', accept='application/json')
        data=json.loads(resp['body'].read()); text=''.join(x.get('text','') for x in data.get('content',[]) if x.get('type')=='text')
        return ModelResponse(text=text, provider=self.provider_name, model=self.model, profile=self.profile_name, usage=data.get('usage'), raw=data)

PROVIDER_CLASSES={
 'openai':OpenAIResponsesProvider,'openai_responses':OpenAIResponsesProvider,
 'openai_compatible':OpenAICompatibleProvider,'openai_chat':OpenAICompatibleProvider,
 'deepseek':OpenAICompatibleProvider,'qwen':OpenAICompatibleProvider,'dashscope':OpenAICompatibleProvider,'groq':OpenAICompatibleProvider,
 'mistral':OpenAICompatibleProvider,'xai':OpenAICompatibleProvider,'openrouter':OpenAICompatibleProvider,'together':OpenAICompatibleProvider,
 'fireworks':OpenAICompatibleProvider,'perplexity':OpenAICompatibleProvider,'moonshot':OpenAICompatibleProvider,'kimi':OpenAICompatibleProvider,'zhipu':OpenAICompatibleProvider,
 'azure_openai':AzureOpenAIProvider,'anthropic':AnthropicProvider,'claude':AnthropicProvider,'gemini':GeminiProvider,'google':GeminiProvider,
 'ollama':OllamaProvider,'cohere':CohereProvider,'baidu_qianfan':BaiduQianfanProvider,'bedrock':BedrockProvider
}
