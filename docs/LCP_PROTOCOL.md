# 📡 LCP: LoLLMs Communication Protocol

The **LoLLMs Communication Protocol (LCP)** is a markup standard used by agents to communicate structured reasoning, technical states, and system-level operations within a text stream. The LoLLMs Hub frontend automatically interprets these tags to provide high-fidelity UI components.

---

## 1. `<think>` (Reasoning Block)
Used for Chain-of-Thought (COT) or internal brainstorming.

- **Frontend Action**: Renders as a collapsible "Internal Reasoning" block.
- **Usage**:
```
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
```
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
```
User: My favorite color is indigo.
AI: Noted. <memory operation="add" title="fav_color" importance="80">User prefers Indigo</memory>
```

---

## 4. `<artifact>` (Visual/File Delivery)
Used to present generated assets (images, documents) to the user.

- **Attributes**:
  - `title`: the artifact title or filename like `document.md`
  - `type`: `text`, `image`, `video`, or some lollms specific text types: `note`, `skill`, `widget`, `form`.
  - `path`: Static URL, a UUID or local filesystem path for artifacts that can't be returned as text.
- **Frontend Action**: Renders as an interactive preview or download button.
- **Usage**:
```
I've generated the diagram for you:
<artifact type="image" path="/static/uploads/diagram_1.png" />
```

Artifacts are used for token economy and are managed by the client app or our agent panel or the playground ui. They are mostly useful with agents. Instead of placing artifacts in the core of the messages, we put them in a specific space and we only show the current version of the artifact. for example if we ask to build a document, then we ask it to update that document, the llm issues a artifact upodate tag with aider SEARCH REPLACE format, the agentic system manages updating the artifact and creating a new version. So that in consecutive steps, the llm doesn't see multiple times that document resulting in a reduction of the number of used tokens, a better attention level since we don't have multiple polluting versions and so a better reasoning quality.

The llm always issues the artefacts, but it is up to the client to do the stripping and filling of the artefact in system message. In our system we recognize them but we don't do anything, we let the client manage this. we only manage them in our own  clients (the panel, and the playground)
---

## 5. `<affective_update>` (Relationship Tracking)
Used by agents to update their internal "emotional" or professional link with the user.

- **Attributes**:
  - `value`: A short descriptive state (e.g., "Friendly", "Hostile", "Professional").
- **Frontend Action**: Updates the Affective Matrix gauge in the dashboard.
```
<affective_update value="Worship/Respect" />
```

## 6. Specific artifacts
