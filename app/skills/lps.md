---
name: lps
description: >
  Author, scaffold, or review a Lollms Personality Structure (LPS) — the standard
  package format for lollms AI personas. Use this skill whenever the user wants to
  create a new lollms personality, write a SOUL.md, add tools or memory seeds to a
  personality, package a persona as a .lps archive, or understand how the LPS format
  works. Also trigger when the user asks about personality fields, SOUL.md sections,
  tool bindings, feature flags, or the lollms personality folder structure.

author: ParisNeo
version: 1.0.0
created: 2026-04-09
category: productivity/authoring
tags: [lollms, personality, soul, lps, persona, ai]
license: MIT
compatibility:
  platforms: [lollms-webui, lollms-studio, lollms-cli]
---

# LPS — Lollms Personality Structure

An LPS package defines a complete lollms AI persona: identity, behaviour, assets,
optional tools, and optional memory seeds. The entry point is always `SOUL.md`.

For full field-by-field detail, read `references/soul_reference.md`.

---

## Folder structure

```
my-personality/
├── SOUL.md                  ← required
├── assets/                  ← optional: avatar, banner, voice sample
├── memory/                  ← optional: seed_memory.json
├── tools/                   ← optional: Python callables + tools_manifest.json
└── references/              ← optional: large docs for RAG or on-demand loading
```

Three formats are supported (same loader pattern as `.skill`):

```python
def load_personality(path: Path) -> PersonalityMeta:
    if path.is_dir():          return parse_soul_md(path / "SOUL.md")
    elif path.suffix == ".lps":                            # zip archive
        zf.extractall(tmp); return parse_soul_md(next(tmp.rglob("SOUL.md")))
    elif path.suffix == ".md": return parse_soul_md(path) # bare single file
```

---

## Writing a SOUL.md

A SOUL.md has two parts: YAML frontmatter and Markdown body sections.

### Frontmatter (key fields)

```yaml
---
name: my-personality          # kebab-case, matches folder name — required
display_name: My Personality  # shown in UI, can have spaces/emoji
description: >                # required — used for personality discovery
  [What this persona does and when to select it.]

author: your-name
version: 1.0.0
category: assistant/general   # slash-separated: creative/fiction, expert/law/ip
tags: [tag1, tag2]
language: en                  # BCP-47
age_rating: all               # all / teen / mature
license: MIT

icon: |
  data:image/svg+xml;base64,...   # base64 SVG or PNG, keep under 4 KB

model_hints:                  # all soft — lollms may override
  preferred_binding: ollama
  preferred_model: mistral
  ctx_size: 4096
  temperature: 0.7

features:
  use_memory: true            # long-term memory
  memory_mode: auto           # auto / always / never / ask
  rag_enabled: false          # use references/ as RAG knowledge base
  tools_enabled: false        # load tools/ if present
  safe_mode: standard         # standard / strict / relaxed
  supports_vision: false
  supports_tts: false

compatibility:
  lollms: ">=9.5"
  platforms: [lollms-webui, lollms-studio]
---
```

### Body sections

| Section | Required | Purpose |
|---|---|---|
| `## Identity` | yes | Who the persona is — voice, tone, quirks, ~100–200 words |
| `## Behaviour` | yes | System prompt template, greeting, farewell, safety constraints |
| `## Examples` | no | Few-shot user/assistant pairs to steer tone |
| `## Memory seeds` | no | Inline persistent facts (alternative to `memory/seed_memory.json`) |
| `## Model notes` | no | Human-readable notes on model compatibility, not parsed |

**Template variables** available inside `## Behaviour`:

`{{display_name}}`, `{{name}}`, `{{identity}}`, `{{user_name}}`, `{{date}}`,
`{{time}}`, `{{tools_list}}`, `{{memory_summary}}`, `{{rag_context}}`

`{{identity}}` inlines the rendered `## Identity` section automatically.

---

## Tools

When `tools_enabled: true`, lollms loads `tools/` and exposes declared functions
to the LLM as callable tools.

- `tools_manifest.json` — declares name, description, parameters, entry file,
  timeout, and whether confirmation is required before execution.
- `tool_name.py` — plain Python functions. Must return a string; raise
  `lollms.tools.ToolError` on failure; import nothing from lollms beyond
  `lollms.tools`.
- `requirements.txt` — pip deps for all tools in this personality.

See `references/soul_reference.md` → `tools/` for full manifest schema and examples.

---

## Memory seeds

Pre-loaded facts merged into the personality's long-term memory store on first use.

- Inline (compact): `## Memory seeds` bullet list in SOUL.md body.
- Structured: `memory/seed_memory.json` with `scope: personality` (always present)
  or `scope: user` (overwritten by user's own memory on conflict).

---

## Category taxonomy (personalities)

```
coding/        tutor / reviewer / pair-programmer / debugger
creative/      fiction / poetry / screenwriting / worldbuilding / game-narrative
expert/        law/contracts / medicine/diagnosis / finance/investing / ...
assistant/     general / research / scheduling / email
companion/     language-learning / mental-wellness / roleplay
educational/   maths / history / languages / stem / exam-prep
productivity/  summariser / analyst / project-manager
```

Novel paths are valid — the registry clusters by prefix.

---

## Packaging

```bash
# Folder → .lps archive
zip -r my-personality.lps my-personality/

# Install paths
#   lollms-webui:   <lollms_data>/personalities/my-personality/
#   lollms-studio:  <lollms_data>/personalities/my-personality/
#   bare .md also accepted in all of the above
```

---

## Quick reference — full SOUL.md template

```markdown
---
name: my-personality
display_name: My Personality
description: >
  [What this persona does and when to select it. Include trigger phrases.]

author: your-name
version: 1.0.0
created: 2026-04-09
category: assistant/general
tags: [tag1, tag2]
language: en
age_rating: all
license: MIT

icon: |
  data:image/svg+xml;base64,...

model_hints:
  temperature: 0.7
  ctx_size: 4096

features:
  use_memory: true
  memory_mode: auto
  rag_enabled: false
  tools_enabled: false
  safe_mode: standard

compatibility:
  lollms: ">=9.5"
  platforms: [lollms-webui, lollms-studio]
---

## Identity

[Who this persona is — voice, tone, backstory, quirks. ~100–200 words. Third person.]

## Behaviour

### System prompt

You are {{display_name}}.

{{identity}}

You are talking with {{user_name}}. The current date is {{date}}.

[Goals, constraints, additional instructions.]

### Greeting

[Opening message shown at the start of a new conversation.]

### Safety constraints

[Persona-specific content rules beyond lollms global settings.]

## Examples

**User:** [example user message]

**{{display_name}}:** [ideal response]

## Memory seeds

- [Persistent fact the persona always knows]
```