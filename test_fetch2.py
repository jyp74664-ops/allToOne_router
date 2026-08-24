import asyncio
import httpx
from services.health_service import fetch_models
from services.meta_service import ModelMetaService

async def test():
    meta = ModelMetaService()
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        # 测试不带认证
        r1 = await client.get('https://api.aionlabs.ai/v1/models')
        print(f'No auth: {r1.status_code}')
        
        # 测试带认证
        r2 = await client.get('https://api.aionlabs.ai/v1/models', headers={'Authorization': 'Bearer test'})
        print(f'With auth: {r2.status_code}')
        
        # 测试 fetch_models
        models = await fetch_models(
            client,
            "https://api.aionlabs.ai/v1",
            "alv2_K4pqPgs0uYAN3cjWyYKsJap0D6PGhZpCib-ESWc6zgg",
            free_only=False,
            aliases=meta.aliases,
            context_limits=meta.context_limits,
        )
        print(f'fetch_models count: {len(models)}')
        print(f'models: {models}')

asyncio.run(test())
