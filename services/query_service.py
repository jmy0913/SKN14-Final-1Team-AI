import json
import chromadb
from chromadb.utils import embedding_functions

from models.query_model import QueryRequest


from pathlib import Path


SERVICES_DIR = Path(__file__).resolve().parent  # services
CHROMA_DIR = SERVICES_DIR / "utils" / "chroma_db"
CHROMA_DIR.mkdir(parents=True, exist_ok=True)


collection = None
doc_ids = []
doc_texts = []
okt = None


def ensure_search_initialized():
    global collection

    if collection is not None:
        return

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    emb = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="BAAI/bge-m3",
        device="cpu",
    )
    collection = client.get_collection(name="google_api_docs", embedding_function=emb)


def normalize_meta(meta, default_doc=""):  # Chroma 메타데이터 정리
    raw_file = meta.get("source_file", "")
    if raw_file.endswith(".txt"):
        title = raw_file[:-4]
    else:
        title = meta.get("title") or raw_file or ""

    source = meta.get("url") or meta.get("source") or ""
    if isinstance(source, list):
        source = (source[0] or "").strip()
    elif isinstance(source, str):
        s = source.strip()
        if s[:1] in "[{":  # JSON 형태라면 url 추출
            try:
                data = json.loads(s)
                if isinstance(data, list) and data:
                    source = str(data[0]).strip()
                elif isinstance(data, dict) and "url" in data:
                    source = str(data["url"]).strip()
            except Exception:
                pass
        else:
            source = s

    snippet = (default_doc or "")[:220]

    return {
        "title": title,
        "source": source,
        "snippet": snippet,
    }


async def search_dense(request: QueryRequest):
    q = request.q
    k = request.k

    ensure_search_initialized()
    res = collection.query(
        query_texts=[q],
        n_results=k * 2,
        include=["documents", "metadatas"],
    )
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    ids = res["ids"][0]

    rows, seen = [], set()
    for i, (doc, meta) in enumerate(zip(docs, metas)):
        norm = normalize_meta(meta or {}, doc)
        key = norm["source"] or (norm["title"], norm["snippet"])
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            {
                "id": ids[i],
                "title": norm["title"],
                "source": norm["source"],
                "snippet": norm["snippet"],
            }
        )
        if len(rows) >= k:
            break
    return rows