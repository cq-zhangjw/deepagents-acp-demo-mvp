from functools import lru_cache

from sentence_transformers import SentenceTransformer, CrossEncoder

# ---------- 1. 初始化嵌入模型 ----------
# 该模型输出 384 维向量，支持 50+ 语言[citation:2][citation:5]
# model = SentenceTransformer('models/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')


@lru_cache(maxsize=4)
def _load_model(embedding_model):
    return SentenceTransformer(embedding_model)


def encode_text(text: str, embedding_model='models/sentence-transformers/granite-embedding-107m-multilingual'):
    model = _load_model(embedding_model)

    return model.encode(text).tolist()


def rerank(query: str, doc_list: list, rerank_model: str = "BAAI/bge-reranker-base"):
    reranker = CrossEncoder(
        rerank_model,
        device="cpu",
    )
    rerank_scores = reranker.predict([(query, r["page_content"]) for r in doc_list])
    reranked_results = [r for _, r in sorted(zip(rerank_scores, doc_list), key=lambda x: x[0], reverse=True)]
    return reranked_results