import asyncio
import os
from api.intent import classify_intent
from api.memory import get_embedding_async, ensure_graph_node

async def test_all():
    print("Testing ML Classification...")
    res = await classify_intent("Remind me to buy milk", "wa_test1", "1", "Testing", 0.0)
    print(res)

    print("\nTesting Embeddings...")
    emb = await get_embedding_async("This is a quick test.")
    print(f"Embedding length: {len(emb)}")

if __name__ == "__main__":
    asyncio.run(test_all())
