import asyncio
import httpx

async def test():
    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        # 测试无认证
        r1 = await client.get('https://api.aionlabs.ai/v1/models')
        print(f'no auth: {r1.status_code}')
        print(r1.text[:300])
        print()
        
        # 测试带认证
        r2 = await client.get('https://api.aionlabs.ai/v1/models', headers={'Authorization': 'Bearer test'})
        print(f'with auth: {r2.status_code}')
        print(r2.text[:300])

asyncio.run(test())
