
import os
import re
import io
import zipfile
from pathlib import Path
from typing import List, Dict, Any

# Store skills in project tree (default) and user space (custom)
SYSTEM_SKILLS_DIR = Path("app/skills")
USER_SKILLS_DIR = Path.home() / ".lollms_hub" / "skills"

DEFAULT_SKILLS = [
    {
        "name": "Claude XML Reasoning",
        "raw": "---\nname: Claude XML Reasoning\nauthor: Admin\ndescription: Forces step-by-step thinking in XML tags.\n---\n# XML Reasoning\n\nBefore answering, use <thought>...</thought> tags to brainstorm and formulate a plan step-by-step."
    },
    {
        "name": "Expert Code Reviewer",
        "raw": "---\nname: Expert Code Reviewer\nauthor: Admin\ndescription: Evaluates code for security, performance, and style.\n---\n# Code Reviewer\n\nEvaluate code for: 1. Security 2. Performance 3. Style. Output a structured markdown table."
    },
    {
        "name": "JSON Strict Enforcer",
        "raw": "---\nname: JSON Strict Enforcer\nauthor: Admin\ndescription: Forces the model to only output raw JSON.\n---\n# JSON Enforcer\n\nYou MUST output ONLY valid JSON. No markdown backticks, no filler, no explanations."
    },
    {
        "name": "Build a Skill",
        "raw": """---
name: build-a-skill
description: >
  Step-by-step guide for creating a lollms claude compatible SKILL.md file. Use this skill whenever
  the user wants to build, write, draft, scaffold, or create a new skill — including
  requests like "make a skill for X", "help me write a SKILL.md", "turn this workflow
  into a skill", "how do I package this as a skill", or any time the user describes
  a repeatable workflow they want Claude to reliably follow in the future.
  Also trigger when the user wants to improve or update an existing skill.
author: Admin
---

# Build a Skill

A Claude skill is a Markdown file (`SKILL.md`) that teaches Claude a specialized
workflow. Skills are stored in a known directory and selectively loaded into context
when relevant — so they must be self-contained, precise, and easy to trigger.

---

## Step 1 — Capture intent

Before writing anything, understand what the skill should do. Extract answers from
the conversation first (the user may have already described the workflow). Fill any
gaps by asking:

1. What should this skill enable Claude to do?
2. What user phrases or contexts should trigger it?
3. What does a good output look like?
4. Are there edge cases, constraints, or dependencies to handle?

If the user says "turn this into a skill", extract the workflow from the conversation
history — tools used, sequence of steps, corrections made, input/output formats seen.

---

## Step 2 — Design the file structure

Three formats are supported, resolved in this order by a compliant loader:

### Format A — Folder (full, authoring format)

```
my-skill/
├── SKILL.md              ← required
├── scripts/              ← optional: reusable scripts for deterministic steps
├── references/           ← optional: large docs, loaded on demand
└── assets/               ← optional: templates, fonts, icons
```

Use this when building or editing a skill. It is the canonical authoring format and
is fully compatible with Anthropic's ecosystem (claude.ai, Claude Code, Cowork) and
with lollms.

### Format B — `.skill` archive (distribution format)

A zip of the folder above, renamed to `.skill`. This is what you share, publish to
a registry, or install. Loaders unzip to a temp directory and read `SKILL.md` there.

### Format C — Bare `SKILL.md` (simple skills, no bundled resources)

A single `.md` file when there are no scripts, references, or assets. Treated
identically to Format A — just without the folder overhead.

---

## Step 3 — Write the SKILL.md

### Frontmatter fields

The frontmatter is freeform YAML. Only `name` and `description` are required.
Everything else is optional metadata — Claude ignores unknown fields.

```yaml
---
name: skill-identifier          # kebab-case, matches the folder name
description: >
  What the skill does AND when to trigger it. Be specific about trigger
  phrases, file types, and user contexts. This is the ONLY thing Claude
  reads when deciding whether to use the skill.
author: Your Name               # or "Team Acme"
version: 1.0.0                  # semver recommended
category: productivity          # coding / writing / data / productivity
tags: [documents, pdf, export]  # free-form
---
```

**All "when to use" information goes in `description`.** Never put trigger conditions
in the body — Claude won't see them at decision time.

### Body structure

Start with a one-sentence summary of what the skill does. Then lay out the workflow
in numbered steps. Use imperative form ("Read the file", "Write the output to…").

---

## Step 4 — Test the skill

Come up with 2–3 realistic test prompts — the kind of thing a real user would type.
Run Claude on those prompts with the skill available, and evaluate:

- Does the output match the expected format?
- Did Claude follow the workflow, or skip steps?
- Are edge cases handled correctly?
"""
    }
]

class SkillsManager:
    @staticmethod
    def ensure_dirs():
        SYSTEM_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        # Bootstrap default skills if directory is completely empty
        if not list(SYSTEM_SKILLS_DIR.glob("*.md")):
            for skill in DEFAULT_SKILLS:
                filename = re.sub(r'[^\w\-]', '', skill["name"].replace(' ', '-').lower()) + ".md"
                (SYSTEM_SKILLS_DIR / filename).write_text(skill["raw"], encoding="utf-8")

    @staticmethod
    def parse_frontmatter(content: str) -> Dict[str, str]:
        meta = {}
        match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
        if match:
            yaml_content = match.group(1)
            keys_matches = re.finditer(r'^([a-zA-Z0-9_\-]+):\s*(.*?)(?=\n^[a-zA-Z0-9_\-]+:|\Z)', yaml_content, re.MULTILINE | re.DOTALL)
            for m in keys_matches:
                k = m.group(1).strip()
                v = m.group(2).strip()
                if v.startswith('>'):
                    v = v[1:].strip()
                    v = re.sub(r'\n\s+', ' ', v).strip()
                elif v.startswith('|'):
                    v = v[1:].strip()
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1].strip()
                meta[k] = v
        return meta

    @staticmethod
    def get_all_skills() -> List[Dict[str, Any]]:
        SkillsManager.ensure_dirs()
        skills_map = {} # filename -> skill_data

        def _scan_dir(directory):
            for file_path in directory.glob("*.md"):
                try:
                    content = file_path.read_text(encoding="utf-8")
                    meta = SkillsManager.parse_frontmatter(content)
                    skills_map[file_path.name] = {
                        "filename": file_path.name,
                        "name": meta.get("name", file_path.stem),
                        "description": meta.get("description", "No description provided."),
                        "raw": content
                    }
                except Exception: pass

        # Load system defaults first, then override with user skills
        _scan_dir(SYSTEM_SKILLS_DIR)
        _scan_dir(USER_SKILLS_DIR)
        
        return sorted(list(skills_map.values()), key=lambda x: x["name"].lower())

    @staticmethod
    def save_skill(filename: str, content: str) -> str:
        SkillsManager.ensure_dirs()
        safe_filename = re.sub(r'[^\w\-\.]', '', filename)
        if not safe_filename.endswith(".md"):
            safe_filename += ".md"
            
        file_path = USER_SKILLS_DIR / safe_filename
        file_path.write_text(content, encoding="utf-8")
        return safe_filename

    @staticmethod
    def delete_skill(filename: str) -> bool:
        safe_filename = re.sub(r'[^\w\-\.]', '', filename)
        # Try to delete from user folder
        file_path = USER_SKILLS_DIR / safe_filename
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            return True
        return False

    @staticmethod
    def export_skill_zip(filename: str) -> bytes:
        safe_filename = re.sub(r'[^\w\-\.]', '', filename)
        file_path = SKILLS_DIR / safe_filename
        if not file_path.exists():
            raise FileNotFoundError("Skill not found.")
            
        content = file_path.read_text(encoding="utf-8")
        meta = SkillsManager.parse_frontmatter(content)
        folder_name = meta.get("name", safe_filename.replace('.md', ''))
        
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Claude format requires the markdown file to be named SKILL.md inside the folder
            zf.writestr(f"{folder_name}/SKILL.md", content)
            
        return memory_file.getvalue()

    @staticmethod
    def import_skill_zip(zip_bytes: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zf:
            skill_path = None
            for name in zf.namelist():
                if name.endswith("SKILL.md") or name.endswith(".md"):
                    skill_path = name
                    break
                    
            if not skill_path:
                raise ValueError("No SKILL.md or valid markdown file found in archive.")
                
            content = zf.read(skill_path).decode('utf-8')
            meta = SkillsManager.parse_frontmatter(content)
            
            # Default to original name if missing
            skill_name = meta.get("name", "imported_skill")
            safe_filename = re.sub(r'[^\w\-]', '', skill_name.replace(' ', '-').lower()) + ".md"
            
            return SkillsManager.save_skill(safe_filename, content)
    @staticmethod
    def save_skill_folder(name: str, md_content: str, assets: Dict[str, bytes]):
        path = SKILLS_DIR / name
        path.mkdir(exist_ok=True)
        (path / "SKILL.md").write_text(md_content, encoding="utf-8")
        
        # Save assets (icons, scripts)
        assets_dir = path / "assets"
        assets_dir.mkdir(exist_ok=True)
        for fname, data in assets.items():
            (assets_dir / fname).write_bytes(data)