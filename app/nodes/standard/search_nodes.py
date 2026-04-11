import json
import logging
from typing import Dict, Any, List
from app.nodes.base import BaseNode
from app.core import knowledge_importer as kit
from app.core.events import event_manager, ProxyEvent
from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

class WebSearchNode(BaseNode):
    node_type = "hub/web_search"
    node_title = "Web Search"
    node_category = "Knowledge & RAG"
    node_icon = "🌐"

    @classmethod
    def get_frontend_js(cls) -> str:
        return """
function NodeWebSearch() {
    this.addInput("Query", "string");
    this.addOutput("Results", "string");
    this.properties = { service: "wikipedia", max_results: 5, full_arxiv: false };
    
    this.addWidget("combo", "Service", this.properties.service, (v) => { 
        this.properties.service = v; 
        pushHistoryState(); 
    }, { values: ["wikipedia", "arxiv", "google"] });
    
    this.addWidget("number", "Max Results", 5, (v) => { 
        this.properties.max_results = v; 
        pushHistoryState();
    }, { min: 1, max: 10 });

    this.addWidget("button", "ℹ️ Documentation", null, () => { showNodeHelp(this.type); });
    
    this.title = "🌐 WEB SEARCH";
    this.color = "#3b82f6";
    this.bgcolor = "#1e3a8a";
    this.size = this.computeSize();
    this.serialize_widgets = true;
}
LiteGraph.registerNodeType("hub/web_search", NodeWebSearch);
"""

    async def execute(self, engine, node: Dict[str, Any], output_slot_idx: int) -> Any:
        query = await engine._resolve_input(node, 0)
        if not query:
            return ""

        service = node["properties"].get("service", "wikipedia")
        count = int(node["properties"].get("max_results", 5))
        
        # --- TELEMETRY: Start Search ---
        event_manager.emit(ProxyEvent(
            event_type="active",
            request_id=engine.request_id,
            model=f"Search: {service.upper()}",
            server="External Node",
            sender=engine.sender,
            error_message=f"Querying {service} for '{str(query)[:30]}...'"
        ))

        raw_results = []
        try:
            if service == "wikipedia":
                raw_results = await run_in_threadpool(kit.search_wikipedia_sync, str(query))
            elif service == "arxiv":
                raw_results = await run_in_threadpool(kit.search_arxiv_sync, str(query), max_results=count)
            elif service == "google":
                raw_results = await run_in_threadpool(kit.search_google_sync, str(query))
        except Exception as e:
            logger.error(f"Search node failed: {e}")
            return f"[Search Error: {str(e)}]"

        if not raw_results:
            return f"[No results found on {service} for query: {query}]"

        # --- FORMATTING & CITATION INJECTION ---
        formatted_sources = []
        llm_context_parts = []
        
        # Only process up to 'count'
        for i, item in enumerate(raw_results[:count]):
            title = item.get("title", "Untitled Result")
            content = item.get("content", "")
            url = item.get("url", "")
            
            source_info = {
                "title": title,
                "content": content,
                "source": f"External: {service.capitalize()}",
                "url": url,
                "score": 1.0,
                "index": i
            }
            formatted_sources.append(source_info)
            llm_context_parts.append(f"[[WEB RESULT {i+1}: {title}]]\nURL: {url}\n{content}")

        # Update Request State for UI citations
        if hasattr(engine.request.state, "sources"):
            engine.request.state.sources.extend(formatted_sources)
        else:
            engine.request.state.sources = formatted_sources

        # Final Telemetry Update
        event_manager.emit(ProxyEvent(
            event_type="active",
            request_id=engine.request_id,
            model="Search Complete",
            server=service,
            sender=engine.sender,
            error_message=f"Retrieved {len(formatted_sources)} relevant results."
        ))

        return f"EXTERNAL SEARCH RESULTS ({service.upper()}):\n\n" + "\n\n---\n\n".join(llm_context_parts)