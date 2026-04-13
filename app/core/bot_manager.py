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
        if not self.app.state.settings.enable_bot_mode:
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

    async def _process_bot_request(self, user_text, username, platform_name, target_workflow):
        """Shared logic to route bot messages through the workflow engine."""
        import json, secrets, re, copy
        from app.api.v1.routes.proxy import _resolve_target, _reverse_proxy
        from app.core.memory_manager import CognitiveMemoryManager
        
        async with AsyncSessionLocal() as db:
            req_id = f"bot_{platform_name}_{secrets.token_hex(4)}"
            user_identifier = f"{platform_name}_{username}"
            
            # 1. Update Short-Term Rolling Memory
            if user_identifier not in self.chat_histories:
                self.chat_histories[user_identifier] = []
                
            self.chat_histories[user_identifier].append({"role": "user", "content": user_text})
            if len(self.chat_histories[user_identifier]) > 10:
                self.chat_histories[user_identifier] = self.chat_histories[user_identifier][-10:]
            
            # 2. Fetch Long-Term Cognitive Memory
            memory_context = await CognitiveMemoryManager.get_memory_context(db, user_identifier, target_workflow)
            asyncio.create_task(CognitiveMemoryManager.reorganize_memories(db, user_identifier, target_workflow))

            # 3. Inject Memory directly into the current prompt to bypass Workflow overwrites
            messages = copy.deepcopy(self.chat_histories[user_identifier])
            messages[-1]["content"] = f"{memory_context}\n\nUSER MESSAGE:\n{user_text}"

            if self.app.state.settings.enable_debug_mode:
                logger.info(f"[DEBUG] BOT INPUT INJECTION:\n{messages[-1]['content']}")

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
            async with message.channel.typing():
                response = await self._process_bot_request(message.content, message.author.name, "discord", config.target_workflow)
                
                # CHUNKING LOGIC: Prevent Discord API 'Content too long' errors
                message_parts = split_message(response)
                for part in message_parts:
                    if part:
                        await message.reply(part)

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
                    response = await self._process_bot_request(event["text"], event["user"], "slack", config.target_workflow)
                    await web_client.chat_postMessage(channel=event["channel"], text=response, thread_ts=event.get("ts"))

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
            if not update.message or not update.message.text: return
            
            user_text = update.message.text
            chat_id = update.message.chat_id
            username = update.message.from_user.username or "tg_user"
            
            # Send placeholder
            placeholder = await update.message.reply_text("✨ Thinking...")

            try:
                async with AsyncSessionLocal() as db:
                    # 1. Resolve target (recursive agent/workflow logic)
                    req_id = f"bot_tg_{secrets.token_hex(4)}"
                    messages = [{"role": "user", "content": user_text}]
                    
                    # Resolve logic (passing dummy request state)
                    resolution = await _resolve_target(db, config.target_workflow, messages, sender=f"Bot:{config.name}")
                    real_model, final_msgs = resolution
                    
                    from app.crud import server_crud
                    servers = await server_crud.get_servers_with_model(db, real_model)
                    
                    if not servers:
                        await placeholder.edit_text("❌ Error: Workflow compute nodes are offline.")
                        return

                    # Create a synthetic request object to satisfy the proxy logic
                    from starlette.requests import Request as StarletteRequest
                    # (Note: In a bot context, we bypass some HTTP specific middlewares)
                    
                    # Call Backend (Non-streaming for bots usually works best)
                    resp, _ = await _reverse_proxy(
                        self.app.state.dummy_request, "chat", servers, 
                        json.dumps({"model": real_model, "messages": final_msgs, "stream": False}).encode(),
                        is_subrequest=True, request_id=req_id, model=config.target_workflow, sender=username
                    )
                    
                    if hasattr(resp, 'body'):
                        data = json.loads(resp.body.decode())
                        answer = data.get("message", {}).get("content", "Empty response.")
                        await placeholder.edit_text(answer, parse_mode='Markdown')
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