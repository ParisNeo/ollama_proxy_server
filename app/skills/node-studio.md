---
name: node-studio
description: >
  Manual and technical guide for building custom nodes for the LoLLMs Hub Architect. 
  Trigger when the user wants to "create a new node", "extend the architect", 
  "build a custom block", or "define node logic".
author: ParisNeo
version: 1.0.0
---

# LoLLMs Node Studio Guide

Nodes in LoLLMs Hub are dual-layer: **Frontend (Javascript)** for the visual graph and **Backend (Python)** for the actual AI logic.

## 1. Frontend (LiteGraph JS)
The frontend defines the UI, pins, and widgets.

```javascript
function MyCustomNode() {
    this.addInput("In", "string");
    this.addOutput("Out", "string");
    this.properties = { multiplier: 2 };
    this.addWidget("number", "Multiplier", 2, (v) => { this.properties.multiplier = v; });
}
MyCustomNode.title = "Custom Node";
LiteGraph.registerNodeType("custom/my_node", MyCustomNode);
```

## 2. Backend (Python)
The backend executes during the workflow run. It has access to the `engine` and other nodes.

```python
class MyNodeLogic(BaseNode):
    node_type = "custom/my_node"
    async def execute(self, engine, node, output_slot_idx):
        val = await engine._resolve_input(node, 0) # Get input from slot 0
        mult = node['properties'].get('multiplier', 1)
        return f"Result: {val} * {mult}"
```

## 3. Communication
LoLLMs Hub matches the `node_type` string between the JS registration and the Python class.

## 4. External Dependencies (PipMaster Protocol)
When generating nodes that require external libraries (e.g., `requests`, `numpy`, `opencv`), you MUST use `pipmaster` to ensure the user has them installed.

### Example in Python Logic:
```python
class AdvancedProcessor(BaseNode):
    node_type = "custom/advanced_proc"
    
    async def execute(self, engine, node, output_slot_idx):
        import pipmaster as pm
        pm.ensure_packages(["pandas", "matplotlib"], verbose=True)
        # Your logic here...
```

## 5. Web Research & Knowledge
If you are building a node for a specific third-party API (e.g., Spotify, Discord), use the provided web-search context to find the latest endpoint schemas and auth requirements.