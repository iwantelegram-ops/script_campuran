import os
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters, idle
from pyrogram.types import Message, ChatPrivileges, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# Load variabel dari file .env
load_dotenv()

# FIX DNS UNTUK TERMUX / ANDROID
import dns.resolver
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ['8.8.8.8', '1.1.1.1']

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("MONGO_DB_NAME")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
OWNER_ID = int(os.getenv("OWNER_ID"))

# Database MongoDB
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]
stats_collection = db["typing_stats"]
admin_collection = db["current_admins"]
config_collection = db["bot_config"]

app = Client("typing_classifier_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Helper Fungsi: Ambil/Buat Konfigurasi Default
async def get_config():
    config = await config_collection.find_one({"type": "settings"})
    if not config:
        default_config = {
            "type": "settings",
            "mode": "day",  # 'day' atau 'date'
            "reset_days": 7,
            "reset_date": 1,
            "reset_hour": 23,
            "reset_minute": 59,
            "max_admins": 1,
            "next_reset": (datetime.now() + timedelta(days=7)).replace(hour=23, minute=59, second=0, microsecond=0).isoformat(),
            "privileges": {
                "can_manage_chat": True,
                "can_delete_messages": True,
                "can_restrict_members": True,
                "can_invite_users": True,
                "can_pin_messages": True,
                "can_manage_video_chats": False
            }
        }
        await config_collection.insert_one(default_config)
        return default_config
    return config

# Fungsi hitung tanggal reset berikutnya
def calculate_next_reset(config):
    now = datetime.now()
    h = config.get("reset_hour", 23)
    m = config.get("reset_minute", 59)
    
    if config.get("mode") == "day":
        days = config.get("reset_days", 7)
        target = (now + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        target_date = config.get("reset_date", 1)
        try:
            target = now.replace(day=target_date, hour=h, minute=m, second=0, microsecond=0)
            if target <= now: raise ValueError
        except ValueError:
            if now.month == 12:
                target = now.replace(year=now.year + 1, month=1, day=target_date, hour=h, minute=m, second=0, microsecond=0)
            else:
                target = now.replace(month=now.month + 1, day=target_date, hour=h, minute=m, second=0, microsecond=0)
    return target.isoformat()

# Helper Fungsi: Hapus pesan otomatis setelah 10 detik
async def auto_delete(message: Message, delay: int = 10):
    await asyncio.sleep(delay)
    try: await message.delete()
    except: pass

# --- TOMBOL UI CONTROL PANEL ---
def main_owner_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Pilih Mode Reset", callback_data="cfg_mode_select")],
        [InlineKeyboardButton("👑 Jumlah Admin Diangkat", callback_data="cfg_max_admins")],
        [InlineKeyboardButton("🛠️ Atur Hak Akses Admin", callback_data="cfg_privileges")],
        [InlineKeyboardButton("📊 Cek Konfigurasi Aktif", callback_data="cfg_view")]
    ])

def mode_selection_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Base Day (Hari & Jam)", callback_data="setmode_day")],
        [InlineKeyboardButton("Base Date (Tanggal & Jam)", callback_data="setmode_date")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")]
    ])

def privileges_menu(privs):
    def indicator(val): return "✅" if val else "❌"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{indicator(privs['can_delete_messages'])} Hapus Pesan", callback_data="toggle_can_delete_messages")],
        [InlineKeyboardButton(f"{indicator(privs['can_restrict_members'])} Restrict/Mute Member", callback_data="toggle_can_restrict_members")],
        [InlineKeyboardButton(f"{indicator(privs['can_invite_users'])} Undang User", callback_data="toggle_can_invite_users")],
        [InlineKeyboardButton(f"{indicator(privs['can_pin_messages'])} Pin Pesan", callback_data="toggle_can_pin_messages")],
        [InlineKeyboardButton(f"{indicator(privs['can_manage_video_chats'])} Kelola Video Chat", callback_data="toggle_can_manage_video_chats")],
        [InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")]
    ])

# --- COMMANDS BERDASARKAN PRIORITAS ---

# 1. Perintah START di DM
@app.on_message(filters.command("start") & filters.private)
async def owner_start(client, message: Message):
    if message.from_user.id != OWNER_ID: return
    await message.reply_text("👋 **Control Panel Typing Bot**", reply_markup=main_owner_menu())

# 2. Perintah MANUAL TRIGGER RESET di DM
@app.on_message(filters.command("test_reset") & filters.private)
async def test_reset_command(client, message: Message):
    if message.from_user.id != OWNER_ID: return
    await message.reply_text("⏳ Memulai simulasi reset...")
    await dynamic_reset_job()
    await message.reply_text("🏁 Selesai.")

# 3. Perintah STATS di Grup (Diletakkan di atas agar dieksekusi duluan sebelum masuk hitungan skor)
@app.on_message(filters.command("typing_stats") & filters.group)
async def check_stats(client, message: Message):
    if message.chat.id != GROUP_CHAT_ID: return
    try:
        user_id = message.from_user.id
        is_authorized = (user_id == OWNER_ID)
        if not is_authorized:
            member = await client.get_chat_member(GROUP_CHAT_ID, user_id)
            if member.status in ["administrator", "creator"]: is_authorized = True

        if not is_authorized:
            rep = await message.reply_text("⛔ **Akses Ditolak!** Khusus Owner & Admin.")
            asyncio.create_task(auto_delete(message, 10))
            asyncio.create_task(auto_delete(rep, 10))
            return

        cursor = stats_collection.find().sort("score", -1).limit(10)
        top_members = await cursor.to_list(length=10)
        if not top_members:
            rep = await message.reply_text("📭 Belum ada data keaktifan.")
            asyncio.create_task(auto_delete(message, 10))
            asyncio.create_task(auto_delete(rep, 10))
            return
        
        response = "📊 **PAPAN PERINGKAT AKTIVITAS GRUP:**\n\n"
        for rank, member in enumerate(top_members, start=1):
            response += f"{rank}. **{member['user_name']}** — {member['score']} pesan\n"
        rep = await message.reply_text(response)
        asyncio.create_task(auto_delete(message, 10))
        asyncio.create_task(auto_delete(rep, 10))
    except Exception as e: print(f"❌ Error /typing_stats: {e}")

# --- PROCESSING CALLBACK BUTTONS ---
@app.on_callback_query()
async def handle_callbacks(client, callback_query: CallbackQuery):
    if callback_query.from_user.id != OWNER_ID: return
    data = callback_query.data
    config = await get_config()

    if data == "back_to_main":
        await callback_query.message.edit_text("👋 **Control Panel Typing Bot**", reply_markup=main_owner_menu())
    elif data == "cfg_view":
        next_r = datetime.fromisoformat(config['next_reset']).strftime('%Y-%m-%d %H:%M:%S')
        mode_text = f"Setiap `{config['reset_days']}` Hari" if config['mode'] == "day" else f"Setiap Tanggal `{config['reset_date']}`"
        text = f"⚙️ **KONFIGURASI BOT AKTIF:**\n\n🔹 Mode Reset: `{config['mode'].upper()}` ({mode_text})\n⏰ Jam Reset: `{config['reset_hour']:02d}:{config['reset_minute']:02d} WIB`\n👑 Jumlah Admin Diangkat: Top `{config['max_admins']}` Teratas\n📅 Reset Berikutnya: `{next_r} WIB`"
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")]]))
    elif data == "cfg_mode_select":
        await callback_query.message.edit_text("⚙️ **Pilih Mode Penjadwalan Reset:**", reply_markup=mode_selection_menu())
    elif data.startswith("setmode_"):
        mode = data.split("_")[1]
        await config_collection.update_one({"type": "settings"}, {"$set": {"mode": mode}})
        if mode == "day":
            await callback_query.message.edit_text("✍️ Masukkan format `hari:jam:menit` (Contoh: `7:23:59`)")
        else:
            await callback_query.message.edit_text("✍️ Masukkan format `tanggal:jam:menit` (Contoh: `1:00:00`)")
    elif data == "cfg_max_admins":
        buttons = [[InlineKeyboardButton(f"{i} Admin", callback_data=f"setmax_{i}")] for i in [1, 2, 3]]
        buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")])
        await callback_query.message.edit_text("👑 **Jumlah Admin Diangkat Bersamaan:**", reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("setmax_"):
        max_v = int(data.split("_")[1])
        await config_collection.update_one({"type": "settings"}, {"$set": {"max_admins": max_v}})
        await callback_query.answer(f"Sukses! Top {max_v} admin.", show_alert=True)
        await callback_query.message.edit_text("👋 **Control Panel Typing Bot**", reply_markup=main_owner_menu())
    elif data == "cfg_privileges":
        await callback_query.message.edit_text("⚙️ **Atur hak akses kapasitas admin baru:**", reply_markup=privileges_menu(config['privileges']))
    elif data.startswith("toggle_"):
        priv_key = data.replace("toggle_", "")
        current_val = config['privileges'][priv_key]
        await config_collection.update_one({"type": "settings"}, {"$set": {f"privileges.{priv_key}": not current_val}})
        updated_config = await get_config()
        await callback_query.message.edit_reply_markup(reply_markup=privileges_menu(updated_config['privileges']))
        await callback_query.answer("Hak akses diperbarui!")

# INPUT TEKS OWNER (Dikecualikan jika teks diawali tanda "/" agar tidak bentrok dengan command)
@app.on_message(filters.private & filters.text & ~filters.command(["start", "test_reset"]))
async def handle_owner_input(client, message: Message):
    if message.from_user.id != OWNER_ID: return
    text = message.text.strip()
    if ":" in text and len(text.split(":")) == 3:
        try:
            val1, hour, minute = map(int, text.split(":"))
            config = await get_config()
            if config["mode"] == "day":
                await config_collection.update_one({"type": "settings"}, {"$set": {"reset_days": val1, "reset_hour": hour, "reset_minute": minute}})
            else:
                await config_collection.update_one({"type": "settings"}, {"$set": {"reset_date": val1, "reset_hour": hour, "reset_minute": minute}})
            updated_config = await get_config()
            new_next_reset = calculate_next_reset(updated_config)
            await config_collection.update_one({"type": "settings"}, {"$set": {"next_reset": new_next_reset}})
            await message.reply_text(f"✅ **Berhasil Disimpan!**\nReset berikutnya: `{new_next_reset}` WIB", reply_markup=main_owner_menu())
        except Exception as e:
            await message.reply_text(f"❌ Input salah. Error: {e}")

# --- TRACK KEAKTIFAN MEMBER (Mengabaikan semua teks yang mengandung Command "/") ---
@app.on_message(filters.group & ~filters.service & ~filters.command("typing_stats"))
async def track_messages(client, message: Message):
    if message.chat.id != GROUP_CHAT_ID: return
    
    # Saringan ekstra: Jika pesan diawali oleh "/" (perintah tidak dikenal), abaikan dari perhitungan skor
    if message.text and message.text.startswith("/"): return

    if message.from_user and not message.from_user.is_bot:
        user_id = message.from_user.id
        user_name = message.from_user.first_name or "User"
        
        await stats_collection.update_one(
            {"user_id": user_id},
            {"$set": {"user_name": user_name}, "$inc": {"score": 1}},
            upsert=True
        )
        print(f"📈 CHAT LOG: {user_name} mengirim pesan (+1 Poin)")

# --- EKSEKUSI RESET UTAMA & PENGANGKATAN ADMIN ---
async def dynamic_reset_job():
    print("⏳ Menjalankan proses pergantian admin...")
    config = await get_config()
    max_admins = config.get("max_admins", 1)
    
    cursor = stats_collection.find({"score": {"$gt": 0}}).sort("score", -1).limit(max_admins)
    winners = await cursor.to_list(length=max_admins)
    
    old_admins = await admin_collection.find().to_list(length=10)
    new_winner_ids = [w["user_id"] for w in winners] if winners else []
    
    for old in old_admins:
        if old["user_id"] not in new_winner_ids:
            try: await app.promote_chat_member(chat_id=GROUP_CHAT_ID, user_id=old["user_id"], privileges=ChatPrivileges(can_manage_chat=False))
            except Exception as e: print(f"⚠️ Gagal mencopot {old['user_name']}: {e}")
    await admin_collection.delete_many({})

    announcement = "📢 **PERGANTIAN ADMIN OTOMATIS PERIODE BARU!** 📢\n\n"
    if winners:
        announcement += f"🏆 **Selamat kepada top {len(winners)} user teraktif!**\nBerikut daftar admin baru:\n\n"
        p = config['privileges']
        for idx, winner in enumerate(winners, start=1):
            uid = winner["user_id"]
            uname = winner["winner_name"] if "winner_name" in winner else winner["user_name"]
            try:
                await app.promote_chat_member(
                    chat_id=GROUP_CHAT_ID, user_id=uid,
                    privileges=ChatPrivileges(
                        can_manage_chat=p['can_manage_chat'], can_delete_messages=p['can_delete_messages'],
                        can_restrict_members=p['can_restrict_members'], can_invite_users=p['can_invite_users'],
                        can_pin_messages=p['can_pin_messages'], can_manage_video_chats=p['can_manage_video_chats']
                    )
                )
                try: await app.set_chat_administrator_custom_title(GROUP_CHAT_ID, uid, f"Top Member {idx} 👑")
                except: pass
                await admin_collection.insert_one({"user_id": uid, "user_name": uname})
                
                # MENTION TAG JUARA
                mention_link = f"[{uname}](tg://user?id={uid})"
                announcement += f"{idx}. {mention_link} — {winner['score']} pesan\n"
            except Exception as e:
                announcement += f"{idx}. **{uname}** (⚠️ Gagal diangkat, cek izin bot!)\n"
    else:
        announcement += "Tidak ada aktivitas pesan periode ini. Posisi admin tetap. 🏝️"

    new_next_reset = calculate_next_reset(config)
    await config_collection.update_one({"type": "settings"}, {"$set": {"next_reset": new_next_reset}})
    announcement += f"\n🔄 *Poin direset ke 0!*\n📅 Reset Berikutnya: `{new_next_reset} WIB`"
    
    try: await app.send_message(chat_id=GROUP_CHAT_ID, text=announcement)
    except: pass
    await stats_collection.delete_many({})

# TIME CHECKER WORKER
async def time_checker_loop():
    while True:
        try:
            config = await get_config()
            target_str = config.get("next_reset")
            if target_str and datetime.now() >= datetime.fromisoformat(target_str):
                await dynamic_reset_job()
        except Exception as e: print(f"❌ Error checker loop: {e}")
        await asyncio.sleep(60)

# RUNNER
async def main():
    await app.start()
    await get_config()
    print("⏰ Engine Time-Checker Aktif (Akurat Berbasis Jam WIB)...")
    print("🤖 Bot Pyrogram Siap Jalankan...")
    asyncio.create_task(time_checker_loop())
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
