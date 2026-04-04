import asyncio
import aiohttp
import json

async def test_ollama():
    url = "http://127.0.0.1:11434/api/chat"
    payload = {
        "model": "llama3.2",
        "messages": [
            {"role": "user", "content": "Hello, are you there?"}
        ],
        "stream": False,
    }
    print(f"Connecting to {url}...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=30) as resp:
                print(f"Status: {resp.status}")
                if resp.status >= 400:
                    print(await resp.text())
                else:
                    data = await resp.json()
                    print("Response:")
                    print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_ollama())
