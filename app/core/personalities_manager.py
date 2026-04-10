import os
import re
import io
import zipfile
from pathlib import Path
from typing import List, Dict, Any
from app.core.skills_manager import SkillsManager

SYSTEM_PERSONALITIES_DIR = Path("app/personalities")
USER_PERSONALITIES_DIR = Path.home() / ".lollms_hub" / "personalities"

class PersonalityManager:
    @staticmethod
    def ensure_dirs():
        SYSTEM_PERSONALITIES_DIR.mkdir(parents=True, exist_ok=True)
        USER_PERSONALITIES_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def get_all_personalities() -> List[Dict[str, Any]]:
        PersonalityManager.ensure_dirs()
        p_map = {}

        def _scan_dir(directory):
            for file_path in directory.glob("*.md"):
                try:
                    content = file_path.read_text(encoding="utf-8")
                    meta = SkillsManager.parse_frontmatter(content)
                    p_map[file_path.name] = {
                        "filename": file_path.name,
                        "name": meta.get("name", file_path.stem),
                        "description": meta.get("description", "No description provided."),
                        "raw": content
                    }
                except Exception: pass
        
        _scan_dir(SYSTEM_PERSONALITIES_DIR)
        _scan_dir(USER_PERSONALITIES_DIR)
        return sorted(list(p_map.values()), key=lambda x: x["name"].lower())

    @staticmethod
    def save_personality(filename: str, content: str) -> str:
        PersonalityManager.ensure_dirs()
        safe_filename = re.sub(r'[^\w\-\.]', '', filename)
        if not safe_filename.endswith(".md"): safe_filename += ".md"
        (USER_PERSONALITIES_DIR / safe_filename).write_text(content, encoding="utf-8")
        return safe_filename

    @staticmethod
    def delete_personality(filename: str) -> bool:
        safe_filename = re.sub(r'[^\w\-\.]', '', filename)
        file_path = USER_PERSONALITIES_DIR / safe_filename
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
            return True
        return False

    @staticmethod
    def export_personality_zip(filename: str) -> bytes:
        # Resolve path
        file_path = (USER_PERSONALITIES_DIR / filename) if (USER_PERSONALITIES_DIR / filename).exists() else (SYSTEM_PERSONALITIES_DIR / filename)
        if not file_path.exists(): raise FileNotFoundError("Personality not found.")
            
        content = file_path.read_text(encoding="utf-8")
        meta = SkillsManager.parse_frontmatter(content)
        folder_name = meta.get("name", file_path.stem)
        
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{folder_name}/SOUL.md", content)
        return memory_file.getvalue()

    @staticmethod
    def import_personality_zip(zip_bytes: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zf:
            # Look for SOUL.md in archive
            soul_path = next((name for name in zf.namelist() if "SOUL.md" in name or name.endswith(".md")), None)
            if not soul_path: raise ValueError("No SOUL.md found.")
            content = zf.read(soul_path).decode('utf-8')
            meta = SkillsManager.parse_frontmatter(content)
            safe_filename = re.sub(r'[^\w\-]', '', meta.get("name", "imported_persona").replace(' ', '-').lower()) + ".md"
            return PersonalityManager.save_personality(safe_filename, content)