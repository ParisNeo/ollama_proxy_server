import logging
from typing import Dict, Any
from sqlalchemy import select
from fastapi.concurrency import run_in_threadpool
from app.nodes.base import BaseNode
from app.core.events import event_manager, ProxyEvent

logger = logging.getLogger(__name__)

class DatastoreNode(BaseNode):
    node_type = "hub/datastore"

    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeDatastore() {
    this.addInput("Query", "string");
    this.addOutput("Context", "string");
    this.properties = { datastore_name: "", top_k: 3 };
    this.dWidget = this.addWidget("combo", "Datastore", this.properties.datastore_name, (v) => {
        this.properties.datastore_name = v;
        pushHistoryState();
    }, { values: datastores_list });
    this.addWidget("number", "Top K", this.properties.top_k, (v) => {
        this.properties.top_k = v;
        pushHistoryState();
    }, { min: 1, max: 20, step: 1 });
    this.addWidget("button", "ℹ️ Help", null, () => { showNodeHelp("hub/datastore"); });
    this.title = "📚 DATASTORE (RAG)";
    this.color = "#0f766e"; // teal-700
    this.bgcolor = "#042f2e"; // teal-950
    this.size = [260, 100];
    this.serialize_widgets = true;
}
NodeDatastore.prototype.onConfigure = function() {
    if(this.dWidget) this.dWidget.value = this.properties.datastore_name;
};
LiteGraph.registerNodeType("hub/datastore", NodeDatastore);
        """

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        props = node.get("properties", {})
        query_text = await engine._resolve_input_by_name(node, "Query")
        if query_text is None: 
            query_text = await engine._resolve_input(node, 0)
            
        if not query_text: return ""
        
        store_name = props.get("datastore_name")
        top_k = props.get("top_k", 3)
        
        from app.database.models import DataStore
        
        res = await engine.db.execute(select(DataStore).filter(DataStore.name == store_name))
        ds = res.scalars().first()
        if not ds: return f"[Datastore {store_name} not found]"
        
        def _run_query():
            import re
            import base64
            import pipmaster as pm
            pm.ensure_packages(["safe-store"])
            from safe_store import SafeStore
            from pathlib import Path
            
            DATASTORE_ASSETS_DIR = Path("app/static/uploads/datastore_assets")
            
            # Map UI name to library key
            v_key = ds.vectorizer_name
            if v_key == "sentense_transformer": v_key = "st"
            elif v_key == "tf_idf": v_key = "tfidf"

            from app.core.config import settings
            v_conf = (ds.vectorizer_config or {}).copy()
            
            # --- CONSISTENCY FIX: Map base_url to host for Ollama ---
            if v_key == "ollama" and "base_url" in v_conf and "host" not in v_conf:
                v_conf["host"] = v_conf.pop("base_url")

            # Internal Auto-Auth logic for existing datastores
            current_url = v_conf.get("host") if v_key == "ollama" else v_conf.get("base_url")
            if not v_conf.get("api_key") and current_url:
                url_str = str(current_url).lower()
                if "localhost" in url_str or "127.0.0.1" in url_str:
                    # Inject the system key from the app state
                    v_conf["api_key"] = engine.request.app.state.system_key

            store = SafeStore(
                db_path=ds.db_path,
                vectorizer_name=v_key,
                vectorizer_config=v_conf,
                chunking_strategy=ds.chunking_strategy,
                chunk_size=ds.chunk_size,
                chunk_overlap=ds.chunk_overlap
            )
            with store:
                results = store.query(query_text, top_k=top_k)
                
            context_parts = []
            for r in results:
                chunk = r.get("chunk_text", "")
                
                # Intercept image tags and append Base64 data securely
                img_tags = re.findall(r'\[IMG:(.*?)\]', chunk)
                for img_file in img_tags:
                    img_path = DATASTORE_ASSETS_DIR / img_file
                    if img_path.exists():
                        try:
                            with open(img_path, "rb") as f:
                                b64 = base64.b64encode(f.read()).decode('utf-8')
                            
                            ext = img_path.suffix.lower()
                            mime = "image/jpeg"
                            if ext == ".png": mime = "image/png"
                            elif ext == ".webp": mime = "image/webp"
                            
                            chunk = chunk.replace(f"[IMG:{img_file}]", f"[IMG_DATA:data:{mime};base64,{b64}]")
                        except Exception as e:
                            logger.error(f"Datastore Image Error: {e}")
                
                context_parts.append(chunk)
            
            return "\n\n---\n\n".join(context_parts)
                
        event_manager.emit(ProxyEvent("active", engine.request_id, f"Datastore ({store_name})", "Local", engine.sender, error_message=f"Querying knowledge base..."))
        try:
            final_context = await run_in_threadpool(_run_query)
            return final_context
        except Exception as e:
            logger.error(f"Datastore query failed: {e}")
            return f"[Datastore query failed: {str(e)}]"
