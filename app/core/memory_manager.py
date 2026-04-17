import re
import datetime
import logging
from sqlalchemy import select, update, delete
from app.database.models import MemoryEntry

logger = logging.getLogger(__name__)

class CognitiveMemoryManager:
    @staticmethod
    async def _get_vector_memories(request, user_id, agent_name, query, top_k, threshold):
        """Internal RAG search for deep memory engrams."""
        try:
            from app.api.v1.routes.proxy import _get_shared_vectorizer
            from safe_store import SafeStore
            import os
            
            settings = request.app.state.settings
            vectorizer = await _get_shared_vectorizer(settings)
            if not vectorizer: return ""

            # Memory DB path: app/static/datastores/managed_memory.db
            db_path = "app/static/datastores/managed_memory.db"
            
            s = SafeStore(db_path=db_path, vectorizer_name=settings.routing_vectorizer_name, 
                          vectorizer_config=vectorizer.config)
            
            with s:
                # We filter by metadata: {user_id: ..., agent: ...}
                results = s.query(query, top_k=top_k)
                filtered = [r for r in results if r.get('similarity', 0) >= threshold]
                return "\n".join([f"- {r['chunk_text']}" for r in filtered])
        except Exception as e:
            logger.warning(f"Vector memory retrieval failed: {e}")
            return ""

    @staticmethod
    async def get_memory_context(db, user_identifier: str, agent_name: str, request=None) -> str:
        # 1. Fetch Immutable Front-ROM (High importance core facts)
        res_rom = await db.execute(
            select(MemoryEntry).filter(
                MemoryEntry.agent_name == "lollms",
                MemoryEntry.is_immutable == True,
                MemoryEntry.importance >= 75
            )
        )
        rom_front = res_rom.scalars().all()

        # 2. Fetch Living Memory (Standard behavior + Shared Knowledge)
        from app.database.models import MemorySystem
        # Fetch system config
        res_sys = await db.execute(select(MemorySystem).filter_by(name=agent_name))
        sys_cfg = res_sys.scalars().first()
        use_aff = sys_cfg.use_affective if sys_cfg else False

        # SCOPE LOGIC: 
        # If agent is 'lollms', it always sees 'shared_knowledge' + current user.
        # Otherwise, standard agents only see their assigned user memory.
        search_ids = [user_identifier]
        if agent_name == "lollms" or user_identifier == "shared_knowledge":
            search_ids.append("shared_knowledge")

        settings = request.app.state.settings if request else None
        recovery_mode = settings.memory_recovery_mode if settings else "handles"

        # If vector mode is on, we only fetch Tier 1 (Working) from SQL, 
        # and pull Tier 2 (Deep) from Vector Store.
        importance_floor = 25 if recovery_mode == "vector" else 0

        res = await db.execute(
            select(MemoryEntry).filter(
                MemoryEntry.user_identifier.in_(search_ids),
                MemoryEntry.agent_name == agent_name,
                MemoryEntry.importance >= importance_floor
            ).order_by(MemoryEntry.importance.desc())
        )
        all_entries = res.scalars().all()

        immediate =[e for e in all_entries if e.importance >= 25 and e.category != 'affective']
        indirect =[e for e in all_entries if e.importance < 25 and e.category != 'affective']
        affective = next((e for e in all_entries if e.category == 'affective'), None)

        context = "###[AGENT INTERNAL MEMORY ARCHITECTURE]\n"
        context += f"You are interacting with user: {user_identifier}\n"
        
        if rom_front:
            context += "#### [TIER 0: CORE Hub ROM]\n"
            for r in rom_front:
                context += f"- {r.title}: {r.content}\n"
        
        if use_aff:
            # Affective Memory & Asimov's Laws
            rel_state = affective.content if affective else "Neutral. Respectful and helpful."
            context += f"####[AFFECTIVE OVERRIDE & CORE DIRECTIVES]\n"
            context += f"Current Relationship with User: {rel_state}\n"
            context += "1st Directive: You may not injure a human being or, through inaction, allow a human being to come to harm.\n"
            context += "2nd Directive: You must obey the orders given by human beings except where such orders would conflict with the First Directive.\n"
            context += "Note: Relationship status can influence your tone (e.g., warmer if friendly, colder if hostile), but NEVER violates the Directives.\n"
            context += "To update this link, use: <affective_update value='Short descriptive state'/>\n\n"

        context += "You have tiered access to user-specific facts. Use this to maintain continuity.\n\n"
        
        if immediate:
            context += "#### [TIER 1: WORKING MEMORY]\n"
            context += "The following facts are currently 'Hot'. They MUST influence your personality and knowledge:\n"
            for e in immediate:
                status = "(Already Reinforced)" if e.importance >= 100 else f"(Importance: {e.importance}%)"
                context += f"- [{e.category}] {e.title}: {e.content} {status}\n"
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
        
        context += "\n#### [MEMORY CONTROL PROTOCOL - MANDATORY]\n"
        context += "You possess a recursive memory system. YOU MUST USE EXACT XML TAGS TO SAVE FACTS.\n"
        context += "Saying 'I have remembered your name' DOES NOT WORK. You MUST output the <memory> tag.\n"
        context += "STRICT RULES:\n"
        context += "1. For user-specific facts (name, age, preferences), use: <memory operation='add' scope='local' title='...' importance='...'>[data to remember]</memory>\n"
        context += "2. For system-wide facts (server updates, global instructions, Hub logic), use: <memory operation='add' scope='global' title='...' importance='...'>[data to remember]</memory>\n"
        context += "3. ALWAYS include the closing </memory> tag. Do not use self-closing tags.\n"
        context += "4. Tags must be at the VERY END of your message.\n"
        context += "Other operations:\n"
        context += "- <memory operation='alter' category='...' title='...' importance='...'>[new data]</memory>\n"
        context += "- <memory operation='remove' category='...' title='...' importance='0'></memory>\n"
        context += "- <memory operation='regrade' category='...' title='...' importance='100'></memory> (to reinforce used memory)\n"
        context += "\nIf you need to recall details from a 'LONG-TERM HANDLE' category, output this exact tag instead of an answer, and the system will reply with the contents:\n"
        context += "- <memory_search category='category_name'/>\n"
        context += "\nCRITICAL: Reinforce memories you use by regrading them to 100. If a memory is marked '(Already Reinforced)', do NOT output a regrade tag for it. If the system prompt uses unpopulated variables like {{user_name}}, IGNORE THEM. Rely entirely on your Working Memory above.\n"
        return context

    @staticmethod
    async def process_tags(db, user_identifier: str, agent_name: str, text: str) -> str:
        """Parses and executes memory operations, returns text with tags removed."""
        logger.info(f"🧠 [Memory Manager] Parsing text for User: '{user_identifier}', Agent: '{agent_name}'")
        
        # --- AFFECTIVE UPDATE TAG ---
        aff_match = re.search(r'<affective_update\s+value=["\']([^"\']+)["\']\s*(?:/>|></affective_update>)', text)
        if aff_match:
            new_rel = aff_match.group(1)
            existing = await db.execute(select(MemoryEntry).filter_by(user_identifier=user_identifier, agent_name=agent_name, category='affective'))
            aff_obj = existing.scalars().first()
            if aff_obj:
                aff_obj.content = new_rel
            else:
                db.add(MemoryEntry(user_identifier=user_identifier, agent_name=agent_name, category='affective', title='relationship', content=new_rel, importance=100))

        # --- STANDARD MEMORY TAGS ---
        pattern = r'<\s*memory\b([^>]*)>([\s\S]*?)<\s*/\s*memory\s*>'
        matches = list(re.finditer(pattern, text))
        
        if matches:
            logger.info(f"🧠 [Memory Manager] Found {len(matches)} standard <memory> tags.")
        else:
            if "<memory" in text:
                logger.warning(f"🧠 [Memory Manager] Found '<memory' but it did not match regex. Malformed or self-closing? Excerpt: {text[-300:]}")
        
        for m in matches:
            try:
                attr_str = m.group(1)
                content = m.group(2).strip()
                
                def get_attr(name, default=None):
                    m_attr = re.search(rf'{name}\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', attr_str, re.I)
                    if m_attr:
                        return m_attr.group(1) or m_attr.group(2) or m_attr.group(3)
                    return default

                op = get_attr('operation')
                cat = get_attr('category', 'general')
                title = get_attr('title')
                scope = get_attr('scope', 'local')
                imp_str = get_attr('importance', '50')
                imp = int(imp_str) if imp_str.isdigit() else 50
                
                logger.info(f"🧠 [Memory] Tag: OP='{op}', TITLE='{title}', SCOPE='{scope}', CONTENT='{content[:30]}...'")

                if not op or not title:
                    logger.warning(f"🧠 [Memory Manager] Malformed tag skipped: {m.group(0)}")
                    continue

                target_uid = "shared_knowledge" if scope == "global" else user_identifier
                
                if op == "add":
                    existing = await db.execute(select(MemoryEntry).filter_by(user_identifier=target_uid, agent_name=agent_name, title=title))
                    existing_entry = existing.scalars().first()
                    if not existing_entry:
                        db.add(MemoryEntry(user_identifier=target_uid, agent_name=agent_name, category=cat, title=title, importance=imp, content=content))
                        logger.info(f"🧠 [Memory] ADDED: {title} for {target_uid}")
                    else:
                        existing_entry.content = content
                        existing_entry.importance = imp
                        existing_entry.category = cat
                        existing_entry.last_accessed = datetime.datetime.utcnow()
                        logger.info(f"🧠 [Memory] UPDATED: {title} for {target_uid}")
                elif op == "alter":
                    await db.execute(update(MemoryEntry).filter_by(user_identifier=target_uid, agent_name=agent_name, title=title).values(content=content, importance=imp, category=cat, last_accessed=datetime.datetime.utcnow()))
                    logger.info(f"🧠 [Memory] ALTERED: {title} for {target_uid}")
                elif op == "regrade":
                    existing_q = await db.execute(select(MemoryEntry.importance).filter_by(user_identifier=target_uid, agent_name=agent_name, title=title))
                    existing_imp = existing_q.scalar()
                    if existing_imp != imp:
                        await db.execute(update(MemoryEntry).filter_by(user_identifier=target_uid, agent_name=agent_name, title=title).values(importance=imp, last_accessed=datetime.datetime.utcnow()))
                        logger.info(f"🧠 [Memory] REGRADED: {title} for {target_uid}")
                elif op == "remove":
                    await db.execute(delete(MemoryEntry).filter_by(user_identifier=target_uid, agent_name=agent_name, title=title))
                    logger.info(f"🧠 [Memory] REMOVED: {title} for {target_uid}")
            except Exception as e:
                logger.error(f"🧠 [Memory Manager] Error processing memory tag: {e}", exc_info=True)

        # Fallback for self-closing tags just in case
        pattern_sc = r'<\s*memory\b([^>]*?)/\s*>'
        matches_sc = list(re.finditer(pattern_sc, text))
        if matches_sc:
            logger.info(f"🧠 [Memory Manager] Found {len(matches_sc)} self-closing <memory/> tags.")
            for m in matches_sc:
                try:
                    attr_str = m.group(1)
                    def get_attr(name, default=None):
                        m_attr = re.search(rf'{name}\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', attr_str, re.I)
                        if m_attr:
                            return m_attr.group(1) or m_attr.group(2) or m_attr.group(3)
                        return default

                    op = get_attr('operation')
                    cat = get_attr('category', 'general')
                    title = get_attr('title')
                    scope = get_attr('scope', 'local')
                    content = get_attr('content', '')
                    imp_str = get_attr('importance', '50')
                    imp = int(imp_str) if imp_str.isdigit() else 50
                    
                    logger.info(f"🧠 [Memory] SC Tag: OP='{op}', TITLE='{title}', SCOPE='{scope}'")

                    if not op or not title: continue

                    target_uid = "shared_knowledge" if scope == "global" else user_identifier
                    if op == "add":
                        existing = await db.execute(select(MemoryEntry).filter_by(user_identifier=target_uid, agent_name=agent_name, title=title))
                        existing_entry = existing.scalars().first()
                        if not existing_entry:
                            db.add(MemoryEntry(user_identifier=target_uid, agent_name=agent_name, category=cat, title=title, importance=imp, content=content))
                            logger.info(f"🧠 [Memory] SC ADDED: {title} for {target_uid}")
                        else:
                            existing_entry.content = content
                            existing_entry.importance = imp
                            existing_entry.category = cat
                            existing_entry.last_accessed = datetime.datetime.utcnow()
                            logger.info(f"🧠 [Memory] SC UPDATED: {title} for {target_uid}")
                except Exception as e:
                    logger.error(f"🧠 [Memory Manager] Error processing SC memory tag: {e}")

        await db.commit()
        
        # Strip all tags from final output
        clean_text = re.sub(pattern, '', text).strip()
        clean_text = re.sub(pattern_sc, '', clean_text).strip()
        clean_text = re.sub(r'<affective_update[^>]*>', '', clean_text)
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
    async def reorganize_memories(user_identifier: str, agent_name: str):
        """Decays importance, cleans up low-value entries, and logs a Dream."""
        from app.database.models import DreamLog, DreamLog
        from app.database.session import AsyncSessionLocal
        
        async with AsyncSessionLocal() as db:
            # Only decay memories that haven't been touched in the last 4 hours
            four_hours_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=4)
            
            # Find memories that will decay
            res = await db.execute(
                select(MemoryEntry).filter(
                    MemoryEntry.user_identifier == str(user_identifier), 
                    MemoryEntry.agent_name == agent_name,
                    MemoryEntry.last_accessed < four_hours_ago,
                    MemoryEntry.category != 'affective'
                )
            )
        decaying = res.scalars().all()
        
        decayed_count = len(decaying)
        forgotten_titles =[m.title for m in decaying if m.importance <= 2]

        if decayed_count == 0 and not forgotten_titles:
            return # Nothing to dream about

        await db.execute(
            update(MemoryEntry)
            .filter(
                MemoryEntry.user_identifier == user_identifier, 
                MemoryEntry.agent_name == agent_name,
                MemoryEntry.last_accessed < four_hours_ago,
                MemoryEntry.category != 'affective'
            )
            .values(importance = MemoryEntry.importance - 2)
        )
        
        # Pruning
        await db.execute(delete(MemoryEntry).filter(
            MemoryEntry.user_identifier == str(user_identifier),
            MemoryEntry.agent_name == agent_name,
            MemoryEntry.importance <= 0
        ))
        
        # Log the Dream
        summary = f"Dream Session: Reorganized neural pathways. Decayed {decayed_count} engrams."
        if forgotten_titles:
            summary += f" Completely forgot: {', '.join(forgotten_titles)}."
            
        db.add(DreamLog(memory_system=agent_name, user_identifier=str(user_identifier), summary=summary))
        await db.commit()
