# 📡 LCP: LoLLMs Communication Protocol

The **LoLLMs Communication Protocol (LCP)** is a markup standard used by agents to communicate structured reasoning, technical states, and system-level operations within a text stream. The LoLLMs Hub frontend automatically interprets these tags to provide high-fidelity UI components.

---

## 1. `<think>` (Reasoning Block)
Used for Chain-of-Thought (COT) or internal brainstorming.

- **Frontend Action**: Renders as a collapsible "Internal Reasoning" block.
- **Usage**:
```xml
<think>
1. I need to search for the current GPU prices.
2. I will compare them with last month's data.
</think>
Here is the analysis...
```

---

## 2. `<processing>` (Workflow Tracker)
Used to show real-time progress of a multi-step task or node execution.

- **Attributes**:
  - `title`: The name of the process (shown in the header).
- **Frontend Action**: Renders as a "Terminal Style" timeline box.
- **Usage**:
```xml
<processing title="Cluster Health Audit">
* Checking Node 1 (RTX 3090)... Online.
* Verifying Database Integrity... Pass.
</processing>
Audit complete.
```

---

## 3. `<memory>` (Cognitive Engram)
Used when an agent saves or updates information in the user's persistent memory core.

- **Attributes**:
  - `operation`: `add`, `alter`, `remove`, or `regrade`.
  - `title`: Short identifier for the fact.
  - `importance`: Integer (0-100).
- **Frontend Action**: Renders as a subtle "Neural Link" badge.
- **Usage**:
```xml
User: My favorite color is indigo.
AI: Noted. <memory operation="add" title="fav_color" importance="80">User prefers Indigo</memory>
```

---

## 4. `<artifact>` (Visual/File Delivery)
Used to present generated assets (images, documents) to the user.

- **Attributes**:
  - `type`: `file`, `image`, or `diagram`.
  - `path`: Static URL, a UUID or local filesystem path.
- **Frontend Action**: Renders as an interactive preview or download button.
- **Usage**:
```xml
I've generated the diagram for you:
<artifact type="image" path="/static/uploads/diagram_1.png" />
```

---

## 5. `<affective_update>` (Relationship Tracking)
Used by agents to update their internal "emotional" or professional link with the user.

- **Attributes**:
  - `value`: A short descriptive state (e.g., "Friendly", "Hostile", "Professional").
- **Frontend Action**: Updates the Affective Matrix gauge in the dashboard.
```xml
<affective_update value="Worship/Respect" />
```