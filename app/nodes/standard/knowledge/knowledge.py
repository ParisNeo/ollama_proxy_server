from typing import Dict, Any
from sqlalchemy import select
from app.nodes.base import BaseNode
from app.database.models import DataStore
from app.database.session import AsyncSessionLocal
from app.core import knowledge_importer as kit
from fastapi.concurrency import run_in_threadpool

class RAGDatastoreNode(BaseNode):
    node_type = "hub/datastore"
    node_title = "RAG Datastore"
    node_category = "Knowledge & RAG"
    node_icon = "📚"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        
        query = await engine._resolve_input(node, 0)
        store_name = node["properties"].get("datastore")
        top_k = int(node["properties"].get("top_k", 3))
        
        if not query or not store_name: return ""
        
        cb = getattr(engine.request.state, "stream_callback", None)
        if cb:
            await cb(f'<processing type="tool_execution" title="RAG Datastore" tool="datastore">\n* Searching datastore \'{store_name}\' for \'{str(query)[:30]}...\'\n')

        async with AsyncSessionLocal() as db:
            res = await db.execute(select(DataStore).filter(DataStore.name == store_name))
            ds = res.scalars().first()
            
        if not ds: return f"[Error: Datastore '{store_name}' not found]"

        V_MAP = {"sentense_transformer": "st", "tf_idf": "tfidf", "ollama": "ollama", "openai": "openai", "cohere": "cohere", "lollms": "lollms"}
        v_key = V_MAP.get(ds.vectorizer_name, "tfidf")

        def _query():
            s = SafeStore(db_path=ds.db_path, vectorizer_name=v_key, vectorizer_config=ds.vectorizer_config or {})
            with s: return s.query(str(query), top_k=top_k)

        raw_results = await run_in_threadpool(_query)
        context_parts = [f"[[SOURCE {i+1}: {r.get('document_title')}]]\n{r.get('chunk_text')}" for i, r in enumerate(raw_results)]
        
        if cb:
            await cb(f'* Found {len(raw_results)} results.\n</processing>\n')
            
        return "\n\n---\n\n".join(context_parts)

class WebSearchNode(BaseNode):
    node_type = "hub/web_search"
    node_title = "Web Search"
    node_category = "Knowledge & RAG"
    node_icon = "🌐"

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        query = await engine._resolve_input(node, 0)
        if not query: return ""
        service = node["properties"].get("service", "wikipedia")
        count = int(node["properties"].get("max_results", 5))

        cb = getattr(engine.request.state, "stream_callback", None)
        if cb:
            await cb(f'<processing type="tool_execution" title="Web Search" tool="{service}">\n* Searching {service} for \'{str(query)[:30]}...\'\n')

        if service == "wikipedia": res = await run_in_threadpool(kit.search_wikipedia_sync, str(query))
        elif service == "arxiv": res = await run_in_threadpool(kit.search_arxiv_sync, str(query), max_results=count)
        elif service == "google": res = await run_in_threadpool(kit.search_google_sync, str(query))
        else: res =[]

        if cb:
            await cb(f'* Extracted {len(res)} results.\n</processing>\n')

        parts = [f"[[WEB: {r['title']}]]\n{r['content']}" for r in res[:count]]
        return "\n\n---\n\n".join(parts)