from langchain_ollama import ChatOllama
import chromadb

print("== LLM test ==")
llm = ChatOllama(model="qwen2.5:7b")
resp = llm.invoke("In one short sentence, what is Marbella known for?")
print(resp.content)

print("\n== Chroma test ==")
client = chromadb.PersistentClient(path="chroma")
col = client.get_or_create_collection("dakota_notes")

try:
    col.delete(ids=["smoke-1"])
except Exception:
    pass

col.add(
    ids=["smoke-1"],
    documents=[
        "Marbella is a coastal city in southern Spain often associated with tourism, expats, and luxury real estate."
    ],
    metadatas=[{"topic": "marbella", "source": "smoke_test"}],
)

res = col.query(query_texts=["Spain coastal city expats"], n_results=1)
print(res)
