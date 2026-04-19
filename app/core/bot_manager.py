import asyncio
import logging
import json
import secrets
from typing import Dict, Any, List, Optional
from sqlalchemy import select
from app.database.session import AsyncSessionLocal
from app.database.models import BotConfig
from app.core.encryption import decrypt_data
import pipmaster as pm

logger = logging.getLogger(__name__)

class BotManager:
    def __init__(self, app):
        self.app = app
        self.active_tasks: Dict[int, asyncio.Task] = {}
        self._shutdown = False
        self.chat_histories: Dict[str, List[Dict[str, str]]] = {}

    async def start_all_active_bots(self):
        """Initial check and start of bots marked as active."""
        # REPAIR MISSION: Use a more robust settings lookup to prevent race conditions
        # during singleton initialization.
        settings = getattr(self.app.state, 'settings', None)
        if not settings:
            from app.main import shared_state
            settings = shared_state.settings
            
        if not settings or not settings.enable_bot_mode:
            logger.debug("Bot Manager: enable_bot_mode is disabled. Skipping startup.")
            return

        async with AsyncSessionLocal() as db:
            res = await db.execute(select(BotConfig).filter(BotConfig.is_active == True))
            configs = res.scalars().all()
            for cfg in configs:
                await self.start_bot(cfg)

    async def start_bot(self, config: BotConfig):
        if config.id in self.active_tasks:
            return

        token = decrypt_data(config.encrypted_token)
        if not token: return

        if config.platform == 'telegram':
            self.active_tasks[config.id] = asyncio.create_task(self._run_telegram_bot(config, token))
        elif config.platform == 'discord':
            self.active_tasks[config.id] = asyncio.create_task(self._run_discord_bot(config, token))
        elif config.platform == 'slack':
            self.active_tasks[config.id] = asyncio.create_task(self._run_slack_bot(config, token))
        elif config.platform == 'whatsapp':
             logger.warning("WhatsApp requires a public webhook. Configure your Meta App to point to /api/bot/whatsapp/webhook")

    async def stop_bot(self, bot_id: int):
        task = self.active_tasks.pop(bot_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _process_bot_request(self, user_text, username, platform_name, target_workflow, attachments=None):
        """Shared logic to route bot messages through the workflow engine."""
        import json, secrets, re, copy
        import base64
        from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
        from app.core.memory_manager import CognitiveMemoryManager
        from app.crud import user_crud
        from app.core import knowledge_importer as kit

        async with AsyncSessionLocal() as db:
            req_id = f"bot_{platform_name}_{secrets.token_hex(4)}"
            # Set platform context for the agent
            self.app.state.dummy_request.state.source_platform = platform_name
            
            # --- CROSS-PLATFORM IDENTITY RESOLUTION ---
            # Try to find a Hub user that matches the bot handle
            hub_user = await user_crud.get_user_by_username(db, username)
            if hub_user:
                # Use the permanent database ID to sync memory across Web/API/Bots
                user_identifier = str(hub_user.id)
                logger.info(f"Bot Handshake: Resolved platform user '{username}' to Hub ID {user_identifier}")
            else:
                # Fallback to prefixed name if the user doesn't have a Hub account
                user_identifier = f"{platform_name}_{username}"
            
            # 1. Update Short-Term Rolling Memory
            if user_identifier not in self.chat_histories:
                self.chat_histories[user_identifier] =[]
                
            # Format message content (handle potential multimodal data)
            if attachments:
                content =[]
                if user_text:
                    content.append({"type": "text", "text": user_text})
                
                doc_files =[]
                class MockFile:
                    def __init__(self, name, data, ct):
                        self.filename = name
                        self._data = data
                        self.content_type = ct
                    async def read(self): return self._data

                for att in attachments:
                    if att['type'] == 'image':
                        b64 = base64.b64encode(att['data']).decode('utf-8')
                        mime = att.get('mime', 'image/jpeg')
                        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                    elif att['type'] == 'doc':
                        doc_files.append(MockFile(att['filename'], att['data'], att.get('mime', 'application/octet-stream')))
                
                if doc_files:
                    try:
                        extracted = await kit.extract_local_file_content(doc_files)
                        if extracted:
                            content.insert(0, {"type": "text", "text": f"Attached Documents:\n{extracted}\n\n"})
                    except Exception as e:
                        logger.error(f"Doc extraction failed: {e}")
                
                if not content:
                    content = user_text
            else:
                content = user_text

            self.chat_histories[user_identifier].append({"role": "user", "content": content})
            if len(self.chat_histories[user_identifier]) > 10:
                self.chat_histories[user_identifier] = self.chat_histories[user_identifier][-10:]
            
            # Memory logic is now handled by the 'hub/agent' node inside the workflow itself.
            # We only manage the rolling history window for consistency.
            messages = copy.deepcopy(self.chat_histories[user_identifier])

            # 4. Agentic Retrieval Loop
            for turn in range(3):
                resolution = await _resolve_target(
                    db, 
                    target_workflow, 
                    messages, 
                    request=self.app.state.dummy_request, 
                    sender=username
                )
                real_model, final_msgs = resolution

                # --- STATIC RESULT INTERCEPTION ---
                # If the workflow produced a result (e.g. from an AGENT or COMPOSER node),
                # return it immediately.
                if real_model == "__result__":
                    final_content = final_msgs[-1]["content"] if final_msgs else "Empty workflow result."
                    
                    # Log the output for debugging
                    if self.app.state.settings.enable_debug_mode:
                        logger.info(f"[DEBUG] BOT WORKFLOW RESULT:\n{final_content}")
                    
                    # Process memory tags from the result
                    clean_res = await CognitiveMemoryManager.process_tags(db, user_identifier, target_workflow, final_content)
                    
                    # Save to short-term history
                    self.chat_histories[user_identifier].append({"role": "assistant", "content": clean_res})
                    return clean_res
                
                from app.crud import server_crud
                servers = await server_crud.get_servers_with_model(db, real_model)
                if not servers: 
                    logger.error(f"Bot check failed: No servers found for resolved model '{real_model}'")
                    return f"❌ Error: Compute nodes offline for '{real_model}'."

                resp, _ = await _reverse_proxy(
                    self.app.state.dummy_request, "chat", servers, 
                    json.dumps({"model": real_model, "messages": final_msgs, "stream": False}).encode(),
                    is_subrequest=True, request_id=req_id, model=target_workflow, sender=username
                )
                
                if hasattr(resp, 'body'):
                    data = json.loads(resp.body.decode())
                    raw_response = data.get("message", {}).get("content", "Empty response.")
                    
                    if self.app.state.settings.enable_debug_mode:
                        logger.info(f"[DEBUG] BOT RAW RESPONSE (Turn {turn}):\n{raw_response}")
                    
                    clean_response = await CognitiveMemoryManager.process_tags(db, user_identifier, target_workflow, raw_response)

                    # Handle internal ROM Digging (RLM Search)
                    dig_match = re.search(r'<memory_dig\s+regex=["\']([^"\']+)["\']\s*(?:/>|></memory_dig>)', raw_response)
                    if dig_match:
                        pattern = dig_match.group(1)
                        # Search Immutable ROM using Regex
                        res_rom = await db.execute(
                            select(MemoryEntry).filter(
                                MemoryEntry.agent_name == "lollms",
                                MemoryEntry.is_immutable == True,
                                MemoryEntry.content.op('REGEXP')(pattern)
                            ).limit(3)
                        )
                        found = res_rom.scalars().all()
                        memory_text = "\n".join([f"RECOVERED ROM: {f.title} - {f.content}" for f in found])
                        
                        # Loop back into context
                        messages.append({"role": "assistant", "content": raw_response})
                        messages.append({"role": "user", "content": f"INTERNAL ROM SEARCH RESULTS:\n{memory_text}\n\nApply this knowledge to your final answer."})
                        continue
                                        
                    # Handle internal memory search requests
                    search_match = re.search(r'<memory_search\s+category=["\']([^"\']+)["\']\s*(?:/>|></memory_search>)', raw_response)
                    if search_match:
                        category = search_match.group(1)
                        search_results = await CognitiveMemoryManager.search_category(db, user_identifier, target_workflow, category)
                        messages.append({"role": "assistant", "content": raw_response})
                        messages.append({"role": "user", "content": f"SYSTEM MEMORY RESULTS FOR '{category}':\n{search_results}\n\nNow, continue answering or perform memory operations."})
                        continue
                    
                    if not clean_response.strip():
                        clean_response = "Memory updated."
                    
                    # Save final output to pure history
                    self.chat_histories[user_identifier].append({"role": "assistant", "content": clean_response})
                    return clean_response
                else:
                    return "⚠️ Error: Request failed."
            return "⚠️ Error: Too many memory turns."

    async def _run_discord_bot(self, config: BotConfig, token: str):
        pm.ensure_packages(["discord.py"], verbose=True)
        import discord
        
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        client = discord.Client(intents=intents)

        def split_message(text, limit=1900):
            """Splits a string into chunks, preferably at newlines."""
            if len(text) <= limit:
                return [text]
            
            chunks = []
            while len(text) > limit:
                # Find the last newline within the limit to avoid breaking formatting
                split_idx = text.rfind('\n', 0, limit)
                if split_idx == -1:
                    # No newline found, force split at the limit
                    split_idx = limit
                
                chunks.append(text[:split_idx].strip())
                text = text[split_idx:].strip()
            
            if text:
                chunks.append(text)
            return chunks

        @client.event
        async def on_message(message):
            if message.author == client.user: return
            
            attachments =[]
            for att in message.attachments:
                try:
                    data = await att.read()
                    mime = att.content_type or 'application/octet-stream'
                    if mime.startswith('image/'):
                        attachments.append({"type": "image", "data": data, "mime": mime})
                    else:
                        attachments.append({"type": "doc", "data": data, "filename": att.filename, "mime": mime})
                except Exception as e:
                    logger.error(f"Failed to read discord attachment: {e}")

            # --- PERMISSION SAFETY: TYPING INDICATOR ---
            typing_active = False
            try:
                # Use a timeout for the typing context to prevent hanging if the loop is laggy
                typing_ctx = message.channel.typing()
                await asyncio.wait_for(typing_ctx.__aenter__(), timeout=2.0)
                typing_active = True
            except discord.errors.Forbidden:
                logger.warning(f"Bot lacks 'Send Messages' or 'Read History' in {message.channel}. Continuing without typing indicator.")
            except Exception as e:
                logger.error(f"Unexpected error in typing indicator: {e}")

            try:
                response = await self._process_bot_request(message.content, message.author.name, "discord", config.target_workflow, attachments)
                
                import re, os
                artifacts = re.findall(r'<artifact\s+.*?path=["\'](.*?)["\'].*?>', response)
                clean_response = re.sub(r'<artifact\s+.*?/>', '', response).strip()
                clean_response = re.sub(r'<artifact\s+.*?>.*?</artifact>', '', clean_response).strip()

                discord_files =[]
                for path in artifacts:
                    local_path = path
                    if path.startswith('/static/'):
                        local_path = os.path.join("app", path.lstrip('/'))
                    if os.path.exists(local_path):
                        discord_files.append(discord.File(local_path))

                if not clean_response and discord_files:
                    clean_response = "Here are your files:"

                # CHUNKING LOGIC: Prevent Discord API 'Content too long' errors
                message_parts = split_message(clean_response)
                for i, part in enumerate(message_parts):
                    if part or (i == len(message_parts)-1 and discord_files):
                        try:
                            if i == len(message_parts) - 1 and discord_files:
                                await message.reply(part, files=discord_files)
                            else:
                                await message.reply(part)
                        except discord.errors.Forbidden:
                            logger.error(f"CRITICAL: Failed to reply to {message.author}. Check bot permissions in this channel.")
                            break 
            finally:
                if typing_active:
                    try:
                        await typing_ctx.__aexit__(None, None, None)
                    except:
                        pass

        try:
            await client.start(token)
        except Exception as e:
            logger.error(f"Discord Bot Error: {e}")

    async def _run_slack_bot(self, config: BotConfig, token: str):
        pm.ensure_packages(["slack_sdk"], verbose=True)
        from slack_sdk.web.async_client import AsyncWebClient
        from slack_sdk.socket_mode.async_client import AsyncSocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.socket_mode.request import SocketModeRequest

        app_token = decrypt_data(config.extra_settings.get("app_token")) if config.extra_settings else None
        if not app_token: 
            logger.error("Slack requires an App Token (xapp-...) in Extra Settings.")
            return

        web_client = AsyncWebClient(token=token)
        sm_client = AsyncSocketModeClient(app_token=app_token, web_client=web_client)

        async def process(client, req: SocketModeRequest):
            if req.type == "events_api":
                event = req.payload["event"]
                if event.get("type") == "message" and not event.get("bot_id"):
                    # Ack immediately
                    await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
                    
                    # Process
                    response = await self._process_bot_request(event.get("text", ""), event["user"], "slack", config.target_workflow, None)
                    
                    import re, os
                    artifacts = re.findall(r'<artifact\s+.*?path=["\'](.*?)["\'].*?>', response)
                    clean_response = re.sub(r'<artifact\s+.*?/>', '', response).strip()
                    clean_response = re.sub(r'<artifact\s+.*?>.*?</artifact>', '', clean_response).strip()
                    
                    await web_client.chat_postMessage(channel=event["channel"], text=clean_response or "Files attached:", thread_ts=event.get("ts"))
                    
                    for path in artifacts:
                        local_path = path
                        if path.startswith('/static/'): local_path = os.path.join("app", path.lstrip('/'))
                        if os.path.exists(local_path):
                            await web_client.files_upload_v2(channel=event["channel"], file=local_path, thread_ts=event.get("ts"))

        sm_client.socket_mode_request_listeners.append(process)
        await sm_client.connect()
        while not self._shutdown: await asyncio.sleep(1)

    async def _run_telegram_bot(self, config: BotConfig, token: str):
        """Standard Telegram integration via python-telegram-bot."""
        pm.ensure_packages(["python-telegram-bot"], verbose=True)
        from telegram import Update
        from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
        from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy, _async_log_usage
        from fastapi import Request

        async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not update.message: return
            
            user_text = update.message.caption or update.message.text or ""
            chat_id = update.message.chat_id
            username = update.message.from_user.username or "tg_user"

            attachments =[]
            if update.message.photo:
                try:
                    photo_file = await update.message.photo[-1].get_file()
                    data = await photo_file.download_as_bytearray()
                    attachments.append({"type": "image", "data": data, "mime": "image/jpeg"})
                except Exception as e: logger.error(f"TG Photo error: {e}")
            if update.message.document:
                try:
                    doc_file = await update.message.document.get_file()
                    data = await doc_file.download_as_bytearray()
                    attachments.append({"type": "doc", "data": data, "filename": update.message.document.file_name, "mime": update.message.document.mime_type or 'application/octet-stream'})
                except Exception as e: logger.error(f"TG Doc error: {e}")

            if not user_text and not attachments: return
            
            # Send placeholder
            placeholder = await update.message.reply_text("✨ Thinking...")

            try:
                response = await self._process_bot_request(user_text, username, "telegram", config.target_workflow, attachments)
                
                import re, os
                artifacts = re.findall(r'<artifact\s+.*?path=["\'](.*?)["\'].*?>', response)
                clean_response = re.sub(r'<artifact\s+.*?/>', '', response).strip()
                clean_response = re.sub(r'<artifact\s+.*?>.*?</artifact>', '', clean_response).strip()

                if not clean_response and artifacts: clean_response = "Here are your files:"

                if clean_response:
                    await placeholder.edit_text(clean_response, parse_mode='Markdown')
                else:
                    await placeholder.delete()
                
                for path in artifacts:
                    local_path = path
                    if path.startswith('/static/'): local_path = os.path.join("app", path.lstrip('/'))
                    if os.path.exists(local_path):
                        with open(local_path, 'rb') as f:
                            if local_path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                                await context.bot.send_photo(chat_id=chat_id, photo=f)
                            else:
                                await context.bot.send_document(chat_id=chat_id, document=f)
            except Exception as e:
                logger.error(f"Bot execution error: {e}")
                await placeholder.edit_text(f"⚠️ Internal Error: {str(e)[:100]}...")

        application = Application.builder().token(token).build()
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        try:
            await application.initialize()
            await application.start()
            await application.updater.start_polling()
            
            while not self._shutdown:
                await asyncio.sleep(1)
                
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()

    async def shutdown(self):
        self._shutdown = True
        for bid in list(self.active_tasks.keys()):
            await self.stop_bot(bid)