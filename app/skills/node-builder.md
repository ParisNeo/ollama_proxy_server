---
name: node-builder
description: Guide for creating custom LiteGraph nodes for the Lollms Hub Workflow Architect.
author: Admin
---

# Building Custom Nodes

Custom nodes live in `app/nodes/custom/*.js`. Each file must register itself with LiteGraph.

## Template

```javascript
function MyCustomNode() {
    this.addInput("In", "string");
    this.addOutput("Out", "string");
    this.properties = { my_value: 0 };
    this.addWidget("number", "Value", 0, (v) => { this.properties.my_value = v; });
}

MyCustomNode.prototype.onExecute = function() {
    const input = this.getInputData(0);
    this.setOutputData(0, input + " processed");
};

LiteGraph.registerNodeType("custom/my_node", MyCustomNode);
```

## Rules
1. **Namespace**: Always use `custom/` prefix for user-built nodes.
2. **Registry**: `LiteGraph.registerNodeType` is mandatory.
3. **Hot Reload**: Add your file to `app/nodes/custom/`, then refresh the Conception page.