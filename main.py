"""
Meshgram: A Meshtastic ↔ Telegram Bridge
-------------------------------------

Bridges messages between a Meshtastic mesh network and a Telegram group,
with a whitelist of allowed nodes.

Features:
- Only allows messages from pre-approved Meshtastic node IDs.
- Supports nicknames for easy identification in Telegram.
- Converts between Meshtastic hex IDs and Telegram decimal IDs.
- Allows targeting specific nodes or broadcasting to all approved nodes.

"""

import os
from dotenv import load_dotenv

import asyncio
import threading
from pubsub import pub
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import meshtastic
import meshtastic.serial_interface

# ==========================
# CONFIGURATION
# ==========================

# Load environment variables from .env file
load_dotenv()

# Read them into constants
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID", "0"))

if not TELEGRAM_BOT_TOKEN or TELEGRAM_GROUP_ID == 0:
    raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_GROUP_ID not set in .env file")

# Allowed nodes: Nickname → Meshtastic Node ID
# Node IDs are in hex format and must start with "!"
ALLOWED_NODES = {
    "MSH1": "!deadbeef",
    "MSH2": "!42babe42"
}

# Inverse mapping for quick lookups: Node ID → Nickname
NODE_ID_TO_NAME = {node_id: name for name, node_id in ALLOWED_NODES.items()}


# ==========================
# GLOBALS
# ==========================

iface = None            # Meshtastic interface
bot_app = None          # Telegram bot application
main_loop = None        # Main asyncio loop for cross-thread execution

# Special Meshtastic broadcast ID (HEX: FFFFFFFF)
MESHTASTIC_BROADCAST_ID = int("ffffffff", 16)

# ==========================
# HELPER FUNCTIONS
# ==========================

def node_id_to_telegram_id(node_id: str) -> str:
    """
    Convert a Meshtastic node ID from hex to decimal (for Telegram).
    Example: "!82235ac6" → "2183355078"
    """
    return str(int(node_id[1:], 16))


def telegram_id_to_node_id(telegram_id: str) -> str:
    """
    Convert a Telegram decimal ID back to Meshtastic hex format.
    Example: "2183355078" → "!82235ac6"
    """
    return "!" + format(int(telegram_id), "08x")


# ==========================
# MESHTASTIC → TELEGRAM
# ==========================

def on_meshtastic_receive(packet, interface):
    """
    Callback for when a Meshtastic packet is received.
    Forwards messages from allowed nodes to Telegram.
    """
    from_id = packet.get("fromId")
    if from_id not in NODE_ID_TO_NAME:
        return  # Ignore messages from unknown nodes

    # Ignore broadcasts (to all nodes)
    if packet.get("to") == MESHTASTIC_BROADCAST_ID:
        return


    # Extract message text
    text = packet.get("decoded", {}).get("payload", b"").decode(errors="ignore").strip()
    if not text:
        return

    sender_name = NODE_ID_TO_NAME.get(from_id, from_id)
    message = f"[{sender_name}] {text}"

    # Send to Telegram in the main loop
    asyncio.run_coroutine_threadsafe(send_to_telegram(message), main_loop)


async def send_to_telegram(message: str):
    """
    Send a message to the configured Telegram group.
    """
    try:
        await bot_app.bot.send_message(chat_id=TELEGRAM_GROUP_ID, text=message)
    except Exception as e:
        print("Failed to send message to Telegram:", e)


# ==========================
# TELEGRAM → MESHTASTIC
# ==========================

async def telegram_to_mesh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle Telegram messages and forward them to Meshtastic.
    Messages must start with '@nickname' (from ALLOWED_NODES keys) or '@all'.
    """
    if update.effective_chat.id != TELEGRAM_GROUP_ID:
        return  # Ignore messages from other chats

    text = update.message.text.strip()
    if not text or not text.startswith("@"):
        return  # Only handle @mentions

    target_name = text[1:].split(" ")[0]  # Extract nickname after '@'
    message_body = text[len(target_name) + 2:]  # Remove "@nickname " from start

    if not message_body:
        return

    # Broadcast to all allowed nodes
    if target_name.lower() == "all":
        for node_id in ALLOWED_NODES.values():
            iface.sendText(message_body, destinationId=node_id)
        return

    # Send to a specific allowed node
    if target_name in ALLOWED_NODES:
        iface.sendText(message_body, destinationId=ALLOWED_NODES[target_name])


# ==========================
# STARTUP
# ==========================

def start_meshtastic():
    """
    Start the Meshtastic interface in a separate thread.
    """
    global iface
    print("Connecting to Meshtastic...")
    iface = meshtastic.serial_interface.SerialInterface()  # Auto-detect port
    pub.subscribe(on_meshtastic_receive, "meshtastic.receive")
    print("Meshtastic connected.")


def main():
    """
    Main entry point. Starts Meshtastic listener and Telegram bot.
    """
    global bot_app, main_loop

    # Start Meshtastic in background thread
    threading.Thread(target=start_meshtastic, daemon=True).start()

    # Configure Telegram bot
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_to_mesh))

    # Save event loop for cross-thread calls
    main_loop = asyncio.get_event_loop()

    print("Private Meshtastic ↔ Telegram bridge running...")
    bot_app.run_polling()


if __name__ == "__main__":
    main()
