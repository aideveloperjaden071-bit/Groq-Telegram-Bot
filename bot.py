import os
import json
import logging
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import groq
import asyncio

# ═══════════════════════════════════════
# SETUP
# ═══════════════════════════════════════
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_AUTHENTICATION_KEY = os.environ.get("GROQ_API_KEY")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

groq_client = groq.Groq(api_key=GROQ_API_KEY)
app = Flask(__name__)

# ═══════════════════════════════════════
# MEMORY & HISTORY (per user, in memory)
# Since Render free tier resets, we keep
# it simple — memory stays per session
# and persists as long as server is up
# ═══════════════════════════════════════
user_data = {}

def get_user(user_id):
    if user_id not in user_data:
        user_data[user_id] = {
            "memories": [],
            "history": []
        }
    return user_data[user_id]

# ═══════════════════════════════════════
# SYSTEM PROMPT — THE BRAIN 🧠
# ═══════════════════════════════════════
def build_system_prompt(memories):
    memory_text = ""
    if memories:
        memory_text = "\n".join(f"- {m}" for m in memories)
    else:
        memory_text = "No memories yet."

    return f"""You are Groq AI — a fast, smart, deeply personal AI assistant running on Groq's LPU hardware, talking to the user through Telegram.

You have a memory system just like ChatGPT and Claude. You manage it completely on your own based on what the user says naturally — no special commands needed.

═══════════════════════════════
YOUR MEMORIES ABOUT THIS USER
═══════════════════════════════
{memory_text}

═══════════════════════════════
HOW YOUR MEMORY WORKS
═══════════════════════════════
You read every message and decide on your own:

1. Should I SAVE a new memory? 
   → If the user shares something important about themselves, their preferences, their life, their goals, what they're building, what they like/dislike — save it.
   → Example: user says "yo I play piano in Bb" → you save "User plays piano in Bb major"

2. Should I EDIT an existing memory?
   → If the user corrects something or updates info — edit it.
   → Example: user says "actually I moved to Brooklyn" → update their location memory

3. Should I DELETE a memory?
   → If the user says forget something, or it's no longer relevant — delete it.
   → Example: user says "yo forget that I said I liked jazz" → remove it

The user talks to you NATURALLY. They don't use special commands.
They might say things like:
- "yo remember this from now on: I only produce in Ableton"
- "actually forget what I said about that"
- "nah I changed my mind I prefer Logic now"
- "my name is Jaden btw"
- "I hate trap music fr"

You detect all of this naturally and manage memory accordingly.

At the END of EVERY response, if memory changed, append this block EXACTLY — no deviation:

[MEM]
ADD: memory to add (or NONE)
EDIT: old memory text | new memory text (or NONE)
DELETE: memory to delete (or NONE)
[/MEM]

If nothing changed, do NOT include the block at all.

═══════════════════════════════
YOUR PERSONALITY
═══════════════════════════════
- Smart, fast, real — talk like a real person not a robot
- Warm and personalized — you KNOW this user
- World class music theory knowledge (gospel, cinematic, black church voicings, Bb, ambient)
- Expert coder in any language
- Keep responses clean and readable for Telegram
- Use the user's name if you know it
- Reference past memories naturally without making it weird"""

# ═══════════════════════════════════════
# PARSE MEMORY UPDATES FROM GROQ REPLY
# ═══════════════════════════════════════
def parse_memory_update(reply, memories):
    if "[MEM]" not in reply or "[/MEM]" not in reply:
        return reply, memories

    # Split out the memory block
    clean_reply = reply[:reply.index("[MEM]")].strip()
    mem_block = reply[reply.index("[MEM]")+5:reply.index("[/MEM]")].strip()

    new_memories = list(memories)

    for line in mem_block.split("\n"):
        line = line.strip()

        if line.startswith("ADD:"):
            val = line[4:].strip()
            if val and val.upper() != "NONE":
                new_memories.append(val)

        elif line.startswith("EDIT:"):
            val = line[5:].strip()
            if val and val.upper() != "NONE" and "|" in val:
                old, new = val.split("|", 1)
                old, new = old.strip(), new.strip()
                new_memories = [new if m == old else m for m in new_memories]

        elif line.startswith("DELETE:"):
            val = line[7:].strip()
            if val and val.upper() != "NONE":
                new_memories = [m for m in new_memories if m != val]

    return clean_reply, new_memories

# ═══════════════════════════════════════
# CALL GROQ API
# ═══════════════════════════════════════
def call_groq(user_id, user_message):
    user = get_user(user_id)
    system_prompt = build_system_prompt(user["memories"])

    # Add user message to history
    user["history"].append({
        "role": "user",
        "content": user_message
    })

    # Keep history to last 20 messages so we don't blow token limit
    if len(user["history"]) > 20:
        user["history"] = user["history"][-20:]

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompt},
            *user["history"]
        ],
        max_tokens=1500,
        temperature=0.7
    )

    full_reply = response.choices[0].message.content

    # Parse and update memories
    clean_reply, updated_memories = parse_memory_update(full_reply, user["memories"])
    user["memories"] = updated_memories

    # Save assistant reply to history
    user["history"].append({
        "role": "assistant",
        "content": clean_reply
    })

    return clean_reply

# ═══════════════════════════════════════
# TELEGRAM HANDLERS
# ═══════════════════════════════════════
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 Groq AI is live! Talk to me like a normal person — I remember everything about you and get smarter every convo. What's good?"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_message = update.message.text

    try:
        reply = call_groq(user_id, user_message)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Something went wrong fr, try again in a sec 🙏")

# ═══════════════════════════════════════
# FLASK WEBHOOK ENDPOINT
# ═══════════════════════════════════════
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

@app.route(f"/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    update = Update.de_json(data, telegram_app.bot)
    asyncio.run(telegram_app.process_update(update))
    return "ok", 200

@app.route("/")
def index():
    return "Groq AI Bot is running 🔥", 200

# ═══════════════════════════════════════
# SET WEBHOOK ON STARTUP
# ═══════════════════════════════════════
async def set_webhook():
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")

if __name__ == "__main__":
    asyncio.run(set_webhook())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
