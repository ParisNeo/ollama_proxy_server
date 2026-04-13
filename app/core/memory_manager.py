import re
import datetime
import logging
from sqlalchemy import select, update, delete
from app.database.models import MemoryEntry

logger = logging.getLogger(__name__)

class CognitiveMemoryManager:
    @staticmethod
    async def get_memory_context(db, user_identifier: str, agent_name: str) -> str:
        res = await db.execute(
            select(MemoryEntry).filter(
                MemoryEntry.user_identifier == user_identifier,
                MemoryEntry.agent_name == agent_name
            ).order_by(MemoryEntry.importance.desc())
        )
        all_entries = res.scalars().all()

        immediate =[e for e in all_entries if e.importance >= 25]
        indirect =[e for e in all_entries if e.importance < 25]

        context = "### [AGENT INTERNAL MEMORY ARCHITECTURE]\n"
        context += f"You are interacting with user: {user_identifier}\n"
        context += "You have tiered access to user-specific facts. Use this to maintain continuity.\n\n"
        
        if immediate:
            context += "#### [TIER 1: WORKING MEMORY]\n"
            context += "The following facts are currently 'Hot'. They MUST influence your personality and knowledge:\n"
            for e in immediate:
                context += f"- [{e.category}] {e.title}: {e.content} (Importance: {e.importance}%)\n"
        else:
            context += "####[TIER 1: WORKING MEMORY]\nNo active memories.\n"
        
        if indirect:
            context += "\n#### [TIER 2: LONG-TERM HANDLES]\n"
            context += "You recall the following categories exist but details are faded:\n"
            cats = {}
            for e in indirect:
                if e.category not in cats: cats[e.category] = []
                cats[e.category].append(e.title)
            for cat, titles in cats.items():
                context += f"- {cat}: ({', '.join(titles)})\n"
        
        context += "\n#### [MEMORY CONTROL PROTOCOL]\n"
        context += "To update your internal state, append memory tags at the END of your response (these are hidden from the user):\n"
        context += "- <memory operation='add' category='user_info' title='name' importance='90'>User name is X.</memory>\n"
        context += "- <memory operation='alter' category='...' title='...' importance='...'>...</memory>\n"
        context += "- <memory operation='remove' category='...' title='...' importance='0'></memory>\n"
        context += "- <memory operation='regrade' category='...' title='...' importance='100'></memory> (to reinforce used memory)\n"
        context += "\nIf you need to recall details from a 'LONG-TERM HANDLE' category, output this exact tag instead of an answer, and the system will reply with the contents:\n"
        context += "- <memory_search category='category_name'/>\n"
        context += "\nCRITICAL: Always reinforce memories you use by regrading them to 100 importance. If the system prompt uses unpopulated variables like {{user_name}}, IGNORE THEM. Rely entirely on your Working Memory above.\n"
        return context

    @staticmethod
    async def process_tags(db, user_identifier: str, agent_name: str, text: str) -> str:
        """Parses and executes memory operations, returns text with tags removed."""
        pattern = r'<memory\s+operation=["\']([^"\']+)["\'](?:\s+category=["\']([^"\']*)["\'])?\s+title=["\']([^"\']+)["\']\s+importance=["\'](\d+)["\']>([\s\S]*?)<\/memory>'
        
        matches = list(re.finditer(pattern, text))
        for m in matches:
            try:
                op = m.group(1)
                cat = m.group(2) or "general"
                title = m.group(3)
                imp = int(m.group(4))
                content = m.group(5).strip()
                
                if op == "add":
                    existing = await db.execute(select(MemoryEntry).filter_by(user_identifier=user_identifier, agent_name=agent_name, title=title))
                    if not existing.scalars().first():
                        db.add(MemoryEntry(user_identifier=user_identifier, agent_name=agent_name, category=cat, title=title, importance=imp, content=content))
                elif op == "alter":
                    await db.execute(update(MemoryEntry).filter_by(user_identifier=user_identifier, agent_name=agent_name, title=title).values(content=content, importance=imp, category=cat, last_accessed=datetime.datetime.utcnow()))
                elif op == "regrade":
                    await db.execute(update(MemoryEntry).filter_by(user_identifier=user_identifier, agent_name=agent_name, title=title).values(importance=imp, last_accessed=datetime.datetime.utcnow()))
                elif op == "remove":
                    await db.execute(delete(MemoryEntry).filter_by(user_identifier=user_identifier, agent_name=agent_name, title=title))
            except Exception as e:
                logger.error(f"Error processing memory tag: {e}")
        
        await db.commit()
        # Remove tags from text to not show to user
        clean_text = re.sub(pattern, '', text).strip()
        # Remove memory_search tags as well so they don't leak
        clean_text = re.sub(r'<memory_search\s+category=["\']([^"\']+)["\']\s*(?:/>|></memory_search>)', '', clean_text).strip()
        return clean_text

    @staticmethod
    async def search_category(db, user_identifier: str, agent_name: str, category: str) -> str:
        """Fetches contents of a specific category and bumps their importance."""
        res = await db.execute(
            select(MemoryEntry).filter(
                MemoryEntry.user_identifier == user_identifier,
                MemoryEntry.agent_name == agent_name,
                MemoryEntry.category == category
            )
        )
        entries = res.scalars().all()
        if not entries:
            return "No entries found in this category."
        
        result = ""
        for e in entries:
            result += f"- Title: {e.title}\n  Content: {e.content}\n  Importance: {e.importance}%\n\n"
            # Automatically bump importance slightly when retrieved
            e.importance = min(100, e.importance + 10)
            e.last_accessed = datetime.datetime.utcnow()
        await db.commit()
        return result

    @staticmethod
    async def reorganize_memories(db, user_identifier: str, agent_name: str):
        """Decays importance and cleans up low-value entries."""
        # Only decay memories that haven't been touched in the last 4 hours
        four_hours_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=4)
        
        await db.execute(
            update(MemoryEntry)
            .filter(
                MemoryEntry.user_identifier == user_identifier, 
                MemoryEntry.agent_name == agent_name,
                MemoryEntry.last_accessed < four_hours_ago
            )
            .values(importance = MemoryEntry.importance - 2) # Slower decay
        )
        # Pruning: Delete anything that hits 0
        await db.execute(delete(MemoryEntry).filter(MemoryEntry.importance <= 0))
        await db.commit()
