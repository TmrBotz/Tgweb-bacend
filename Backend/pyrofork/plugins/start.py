from asyncio import Queue, Lock, create_task, sleep as asleep, create_subprocess_exec, gather
from os import path as ospath, execl as osexecl
from sys import executable
from urllib.parse import urlparse
import secrets
import string
from datetime import datetime, timedelta

from aiofiles import open as aiopen
from passlib.context import CryptContext
from pyrogram import filters, Client, enums
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from Backend import db
from Backend.config import Telegram
from Backend.helper.custom_filter import CustomFilters
from Backend.helper.encrypt import decode_string
from Backend.helper.pyro import get_readable_file_size
from Backend.helper.titles import clean_title, match_collections
from Backend.logger import LOGGER
from Backend.pyrofork import StreamBot


pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def generate_password(length=10):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


# ----------------------------------------------------------------------
# Owner commands
# ----------------------------------------------------------------------
@StreamBot.on_message(filters.command("user") & filters.private & CustomFilters.owner)
async def create_user(bot: Client, message: Message):
    try:
        args = message.text.split()
        if len(args) != 3:
            await message.reply_text("❌ Usage: `/user <username> <expiry_days>`", parse_mode=ParseMode.MARKDOWN)
            return

        username = args[1]
        expiry_days = int(args[2])

        users_collection = db.db["auth_users"]

        existing_user = await users_collection.find_one({"username": username})
        if existing_user:
            await message.reply_text(f"❌ User `{username}` already exists!", parse_mode=ParseMode.MARKDOWN)
            return

        password = generate_password()
        hashed_password = pwd_ctx.hash(password)
        expires_at = datetime.utcnow() + timedelta(days=expiry_days)

        await users_collection.insert_one({
            "username": username,
            "password": hashed_password,
            "expires_at": expires_at
        })

        await message.reply_text(
            f"✅ User created!\n\n"
            f"👤 Username: `{username}`\n"
            f"🔑 Password: `{password}`\n"
            f"🕒 Expires in: `{expiry_days}` days\n"
            f"📅 Expiry Date: `{expires_at.strftime('%Y-%m-%d %H:%M:%S')} UTC`",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        LOGGER.error(f"Error in /user command: {e}")
        await message.reply_text("❌ An error occurred while creating the user.")


@StreamBot.on_message(filters.command('restart') & filters.private & CustomFilters.owner)
async def restart(bot: Client, message: Message):
    try:
        restart_message = await message.reply_text(
            '<blockquote>⚙️ Restarting Backend API... \n\n✨ Please wait as we bring everything back online! 🚀</blockquote>',
            quote=True,
            parse_mode=enums.ParseMode.HTML
        )
        LOGGER.info("Restart initiated by owner.")

        proc1 = await create_subprocess_exec('python3', 'update.py')
        await gather(proc1.wait())

        async with aiopen(".restartmsg", "w") as f:
            await f.write(f"{restart_message.chat.id}\n{restart_message.id}\n")

        osexecl(executable, executable, "-m", "Backend")

    except Exception as e:
        LOGGER.error(f"Error during restart: {e}")
        await message.reply_text("**❌ Failed to restart. Check logs for details.**")


@StreamBot.on_message(filters.command('log') & filters.private & CustomFilters.owner)
async def send_log(bot: Client, message: Message):
    try:
        path = ospath.abspath('log.txt')
        return await message.reply_document(
            document=path, quote=True, disable_notification=True
        )
    except Exception as e:
        LOGGER.error(f"An error occurred while sending log: {e}")


@StreamBot.on_message(filters.command('delete') & filters.private & CustomFilters.owner)
async def delete(bot: Client, message: Message):
    """
    /delete <id>
    /delete https://domain/watch/<id>      (or /dl/<id>/<name>, /thumb/<id>)
    <id> = the long encoded id, or the short id from the bot link (the part after file_)
    """
    try:
        split_text = message.text.split()
        if len(split_text) != 2:
            return await message.reply_text("Use this format: /delete <id>  or  /delete https://domain/watch/<id>")

        arg = split_text[1]
        identifier = arg
        if arg.lower().startswith("http"):
            parts = [p for p in urlparse(arg).path.split('/') if p]
            if len(parts) >= 2 and parts[0] in ("watch", "dl", "thumb"):
                identifier = parts[1]
            else:
                return await message.reply_text("The URL format is incorrect.")
        elif arg.startswith("file_"):
            identifier = arg[len("file_"):]

        if await db.delete_media(identifier):
            return await message.reply_text("Media has been deleted successfully.")
        return await message.reply_text("That media wasn't found in the database.")

    except Exception as e:
        await message.reply_text(f"An error occurred: {str(e)}")


# ----------------------------------------------------------------------
# /start file_<id>  -> send the file to the user
# ----------------------------------------------------------------------
async def delete_messages_after_delay(messages):
    await asleep(300)
    for msg in messages:
        try:
            await msg.delete()
        except Exception as e:
            LOGGER.error(f"Error deleting message {msg.id}: {e}")
        await asleep(2)


@StreamBot.on_message(filters.command('start') & filters.private)
async def start(bot: Client, message: Message):
    LOGGER.info(f"Received command: {message.text}")

    args = message.command  # ['start', 'file_<id>']
    if len(args) < 2 or not args[1].startswith("file_"):
        await message.reply_text("Hello 👋")
        return

    identifier = args[1][len("file_"):].strip()
    try:
        doc = await db.get_media(identifier)
    except Exception as e:
        LOGGER.error(f"DB error while reading {identifier}: {e}")
        await message.reply_text("Something went wrong, try again later.")
        return

    if not doc:
        await message.reply_text("File not found.")
        return

    sent_messages = []
    try:
        decoded = await decode_string(doc["id"])
        channel = int(f"-100{decoded['chat_id']}")
        msg_id = int(decoded['msg_id'])

        file = await bot.get_messages(channel, msg_id)
        media = file.document or file.video
        if not media:
            await message.reply_text("Error retrieving media.")
            return

        sent_msg = await message.reply_cached_media(
            file_id=media.file_id,
            caption=doc.get("title", "")[:1024]
        )
        sent_messages.append(sent_msg)

    except FloodWait as e:
        LOGGER.info(f"Sleeping for {e.value}s")
        await asleep(e.value)
        await message.reply_text(f"Got Floodwait of {e.value}s, please try again.")
        return
    except Exception as e:
        LOGGER.error(f"Error retrieving/sending media: {e}")
        await message.reply_text("Error retrieving media.")
        return

    warning_msg = await message.reply_text(
        "Forward these files to your saved messages. These files will be deleted from the bot within 5 minutes."
    )
    sent_messages.append(warning_msg)
    create_task(delete_messages_after_delay(sent_messages))


# ----------------------------------------------------------------------
# New file in AUTH_CHANNEL  ->  title + collections + thumbnail flag -> DB
# ----------------------------------------------------------------------

# Global queue for processing file updates
file_queue = Queue()

# Global lock for database access
db_lock = Lock()


async def process_file():
    while True:
        item = await file_queue.get()
        try:
            async with db_lock:
                result = await db.insert_media(**item)

            if result and result["created"]:
                LOGGER.info(f"Saved: {item['title'][:60]!r} -> {item['collections']}")
            elif result:
                LOGGER.info(f"Already in database, skipped: {item['title'][:60]!r}")
            else:
                LOGGER.info("Save failed (see errors above).")
        except Exception as e:
            LOGGER.error(f"Error while saving file to database: {e}")
        finally:
            file_queue.task_done()


# Start the file processing worker
create_task(process_file())


@StreamBot.on_message(
    filters.channel
    & (
        filters.document
        | filters.video
    )
)
async def file_receive_handler(bot: Client, message: Message):
    if str(message.chat.id) not in Telegram.AUTH_CHANNEL:
        await message.reply(text="Channel is not in AUTH_CHANNEL")
        return

    try:
        file = message.video or message.document
        if not file:
            return

        # Title = full caption (only @usernames and links are removed).
        # No caption -> use the file name.
        raw_title = message.caption or file.file_name or ""
        title = clean_title(raw_title) or file.file_name or file.file_unique_id

        # Which collections does this title belong to? (no match -> default one)
        collections = match_collections(title, Telegram.COLLECTIONS, Telegram.DEFAULT_COLLECTION)

        await file_queue.put({
            "title": title,
            "collections": collections,
            "hash": file.file_unique_id[:6],
            "channel": int(str(message.chat.id).replace("-100", "")),
            "msg_id": message.id,
            "size": get_readable_file_size(file.file_size),
            "file_name": file.file_name or "video.mkv",
            "mime_type": file.mime_type or "",
            "file_unique_id": file.file_unique_id,
            "has_thumb": bool(getattr(file, "thumbs", None)),
        })

    except FloodWait as e:
        LOGGER.info(f"Sleeping for {str(e.value)}s")
        await asleep(e.value)
    except Exception as e:
        LOGGER.error(f"Error while handling channel file: {e}")
