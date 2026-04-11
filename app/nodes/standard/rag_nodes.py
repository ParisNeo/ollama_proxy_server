from typing import Dict, Any
from sqlalchemy import select
from app.nodes.base import BaseNode
from app.database.models import DataStore
from app.database.session import AsyncSessionLocal

class RAGDatastoreNode(BaseNode):
    node_type = "hub/datastore"
    node_title = "RAG Datastore"
    node_category = "Knowledge & RAG"
    node_icon = "📚"

    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeDatastore() {
    this.addInput("Query", "string");
    this.addOutput("Context", "string");
    this.properties = { datastore: "", top_k: 3 };
    this.dsWidget = this.addWidget("combo", "Store", this.properties.datastore, (v) => { this.properties.datastore = v; pushHistoryState(); }, { values: window.datastores_list || [] });
    this.addWidget("number", "Top K", 3, (v) => { this.properties.top_k = v; }, { min: 1, max: 20 });
    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    this.title = "📚 RAG DATASTORE";
    this.color = "#0d9488";
    this.bgcolor = "#115e59";
    this.size = this.computeSize();
    this.serialize_widgets = true;
}
NodeDatastore.prototype.onConfigure = function() {
    if(this.dsWidget) this.dsWidget.value = this.properties.datastore;
};
LiteGraph.registerNodeType("hub/datastore", NodeDatastore);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        import pipmaster as pm
        pm.ensure_packages(["safe-store"])
        from safe_store import SafeStore
        from app.core.events import event_manager, ProxyEvent
        
        query = await engine._resolve_input(node, 0)
        store_name = node["properties"].get("datastore")
        top_k = int(node["properties"].get("top_k", 3))
        
        if not query or not store_name:
            return ""

        async with AsyncSessionLocal() as db:
            res = await db.execute(select(DataStore).filter(DataStore.name == store_name))
            ds = res.scalars().first()
            
        if not ds:
            return f"[Error: Datastore '{store_name}' not found]"

        V_MAP = {"sentense_transformer": "st", "tf_idf": "tfidf", "ollama": "ollama", "openai": "openai", "cohere": "cohere", "lollms": "lollms"}
        v_key = V_MAP.get(ds.vectorizer_name, "tfidf")

        def _query():
            s = SafeStore(
                db_path=ds.db_path, 
                vectorizer_name=v_key, 
                vectorizer_config=ds.vectorizer_config or {},
                chunking_strategy=ds.chunking_strategy,
                chunk_size=ds.chunk_size,
                chunk_overlap=ds.chunk_overlap
            )
            with s:
                # Based on safe_store docs: use .query(), returns list of dicts
                return s.query(str(query), top_k=top_k)

        from fastapi.concurrency import run_in_threadpool
        raw_results = await run_in_threadpool(_query)
        
        if not raw_results:
            return ""

        # --- FIRST CLASS RAG: Metadata Extraction & Telemetry ---
        formatted_sources = []
        llm_context_parts = []
        
        for i, r in enumerate(raw_results):
            content = r.get('chunk_text', '')
            score = r.get('similarity', 0.0)
            # Hardened metadata extraction: ensures meta is never None
            meta = r.get('document_metadata') or {}
            
            # Extract title using priority chain from Developer Reference §14
            title = (meta.get("title") or r.get("document_title") or meta.get("filename") or 
                    f"Source {i+1} (Score: {score:.2f})")
            
            source_info = {
                "title": title,
                "content": content,
                "source": store_name,
                "score": float(score),
                "index": i
            }
            formatted_sources.append(source_info)
            
            # Build the text block for the LLM
            llm_context_parts.append(f"[[SOURCE {i+1}: {title}]]\n{content}")

        # 1. Update Request State for the Proxy to return later
        if hasattr(engine.request.state, "sources"):
            engine.request.state.sources.extend(formatted_sources)
        else:
            engine.request.state.sources = formatted_sources

        # 2. Emit SSE Event for Live Flow Dashboard
        event_manager.emit(ProxyEvent(
            event_type="active", # Yellow pulse in Live Flow
            request_id=engine.request_id,
            model="RAG Processor",
            server=store_name,
            sender=engine.sender,
            error_message=f"Retrieved {len(formatted_sources)} chunks from '{store_name}'"
        ))
        
        # 3. Emit detailed Sources List event (MSG_TYPE_SOURCES_LIST = 27)
        # We manually use the active callback stashed on the discussion/request if available
        if hasattr(engine.request.app.state, "event_manager"):
            # This is a specific Hub event to notify the UI to show the source cards
            import json
            from lollms_client.lollms_types import MSG_TYPE
            event_manager.broadcast_to_subscribers({
                "type": "sources_list",
                "id": engine.request_id,
                "data": formatted_sources
            })

        return "\n\n---\n\n".join(llm_context_parts)