import os
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters, idle
from pyrogram.types import Message, ChatPrivileges, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait

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
OWNER_ID = int(os.getenv("OWNER_ID"))

# Database MongoDB
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]
stats_collection = db["typing_stats"]
admin_collection = db["current_admins"]
config_collection = db["bot_config"]

# State sementara untuk mencatat sesi setting admin di private chat
USER_STATES = {}

app = Client("typing_classifier_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

HARI_MAP = {0: "Senin", 1: "Selasa", 2: "Rabu", 3: "Kamis", 4: "Jumat", 5: "Sabtu", 6: "Minggu"}

# Kamus translasi agar nama database berubah jadi bahasa Indonesia di tombol UI
PRIVILEGE_LABELS = {
    "can_delete_messages": "Hapus Pesan",
    "can_restrict_members": "Mute / Blokir (Kick)",
    "can_invite_users": "Undang Anggota",
    "can_pin_messages": "Sematkan (Pin) Pesan",
    "can_manage_video_chats": "Kelola Obrolan Video"
}

# Helper Fungsi: Ambil/Buat Konfigurasi per Grup (Multi-Grup)
async def get_config(chat_id: int):
    try:
        config = await config_collection.find_one({"chat_id": chat_id})
        if not config:
            default_config = {
                "chat_id": chat_id,
                "mode": "day",  # 'day', 'date', atau 'weekday'
                "reset_days": 7,
                "reset_date": 1,
                "reset_weekday": 0,
                "reset_hour": 23,
                "reset_minute": 59,
                "max_admins": 1,
                "next_reset": (datetime.now() + timedelta(days=7)).replace(hour=23, minute=59, second=0, microsecond=0).isoformat(),
                "privileges": {
                    "can_delete_messages": True,
                    "can_restrict_members": True,
                    "can_invite_users": True,
                    "can_pin_messages": True,
                    "can_manage_video_chats": False
                }
            }
            await config_collection.insert_one(default_config)
            return default_config
        
        # Bersihkan sisa-sisa data rusak "can" di database jika ada
        if "privileges" in config and "can" in config["privileges"]:
            await config_collection.update_one({"chat_id": chat_id}, {"$unset": {"privileges.can": ""}})
            config = await config_collection.find_one({"chat_id": chat_id})
            
        return config
    except Exception as e:
        print(f"❌ Error get_config: {e}")
        return {}

# Fungsi hitung tanggal reset berikutnya
def calculate_next_reset(config):
    try:
        now = datetime.now()
        h = config.get("reset_hour", 23)
        m = config.get("reset_minute", 59)
        mode = config.get("mode", "day")
        
        if mode == "day":
            days = config.get("reset_days", 7)
            target = (now + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)
        elif mode == "date":
            target_date = config.get("reset_date", 1)
            try:
                target = now.replace(day=target_date, hour=h, minute=m, second=0, microsecond=0)
                if target <= now: raise ValueError
            except ValueError:
                if now.month == 12:
                    target = now.replace(year=now.year + 1, month=1, day=target_date, hour=h, minute=m, second=0, microsecond=0)
                else:
                    target = now.replace(month=now.month + 1, day=target_date, hour=h, minute=m, second=0, microsecond=0)
        else:
            target_weekday = config.get("reset_weekday", 0)
            days_ahead = target_weekday - now.weekday()
            if days_ahead <= 0: days_ahead += 7
            target = (now + timedelta(days=days_ahead)).replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now: target += timedelta(days=7)
                
        return target.isoformat()
    except Exception as e:
        print(f"❌ Error calculate_next_reset: {e}")
        return datetime.now().isoformat()

# Helper Fungsi: Hapus pesan otomatis setelah 10 detik
async def auto_delete(message: Message, delay: int = 10):
    await asyncio.sleep(delay)
    try: await message.delete()
    except: pass

# Cek apakah user adalah admin di grup tersebut
async def is_user_admin(chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID: return True
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except FloodWait as f:
        await asyncio.sleep(f.value)
        return await is_user_admin(chat_id, user_id)
    except Exception:
        return False

# --- MENU DENGAN PILIHAN GRUP ---
def group_selection_menu(groups):
    buttons = []
    for g in groups:
        buttons.append([InlineKeyboardButton(f"👥 {g['title']}", callback_data=f"manage_{g['chat_id']}")])
    if not buttons:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Belum masuk grup manapun", callback_data="none")]])
    return InlineKeyboardMarkup(buttons)

def main_owner_menu(chat_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Mode Reset", callback_data=f"cfg_mode_select_{chat_id}"),
         InlineKeyboardButton("👑 Kuota Admin", callback_data=f"cfg_max_admins_{chat_id}")],
        [InlineKeyboardButton("🔰 Izin Admin Baru", callback_data=f"cfg_privs_{chat_id}"),
         InlineKeyboardButton("📊 Cek Konfigurasi Aktif", callback_data=f"cfg_view_{chat_id}")],
        [InlineKeyboardButton("🔙 Pilih Grup Lain", callback_data="back_to_groups")]
    ])

def privileges_menu(privs, chat_id: int):
    buttons = []
    for key, val in privs.items():
        if key == "can_manage_chat" or key == "can":
            continue
        status_mark = "🟢 ON" if val else "🔴 OFF"
        label = PRIVILEGE_LABELS.get(key, key)
        buttons.append([InlineKeyboardButton(f"{label}: {status_mark}", callback_data=f"toggle_{key}_{chat_id}")])
    
    buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data=f"manage_{chat_id}")])
    return InlineKeyboardMarkup(buttons)

# --- COMMAND HANDLERS ---
@app.on_message(filters.command("start") & filters.private)
async def owner_start(client, message: Message):
    try:
        user_id = message.from_user.id
        if user_id != OWNER_ID: return
        USER_STATES.pop(user_id, None)
        
        # Cari semua grup aktif yang terdaftar di database
        cursor = config_collection.find()
        all_configs = await cursor.to_list(length=100)
        
        valid_groups = []
        for cfg in all_configs:
            cid = cfg.get("chat_id")
            if cid:
                try:
                    chat = await client.get_chat(cid)
                    valid_groups.append({"chat_id": cid, "title": chat.title})
                except: pass

        welcome_text = (
            "👋 **Halo Boss! Selamat Datang di Panel Manajemen Typing Bot!**\n\n"
            "Bot ini dirancang khusus untuk memantau member teraktif di grup Anda "
            "dan memberikan reward otomatis berupa jabatan **Admin Sementara** secara berkala.\n\n"
            "🛡️ **Fitur Unggulan:**\n"
            "• Menghitung skor chat member secara akurat.\n"
            "• Anti-Spam: Pesan yang langsung dihapus cepat tidak akan dihitung.\n"
            "• Kontrol penuh hak kekuasaan admin baru lewat tombol ON/OFF.\n\n"
            "👇 **Silakan pilih grup yang ingin Anda kelola di bawah ini:**"
        )
        await message.reply_text(welcome_text, reply_markup=group_selection_menu(valid_groups))
    except Exception as e: print(f"💥 Crash /start: {e}")

@app.on_message(filters.command("typing_stats") & filters.group)
async def check_stats(client, message: Message):
    try:
        chat_id = message.chat.id
        if not await is_user_admin(chat_id, message.from_user.id if message.from_user else 0): return
        
        cursor = stats_collection.find({"chat_id": chat_id}).sort("score", -1).limit(10)
        top_members = await cursor.to_list(length=10)
        if not top_members:
            rep = await message.reply_text("📭 Belum ada data keaktifan.")
            asyncio.create_task(auto_delete(message, 10)); asyncio.create_task(auto_delete(rep, 10))
            return
        response = f"📊 **PAPAN PERINGKAT AKTIVITAS GRUP**\n\n"
        for rank, member in enumerate(top_members, start=1):
            response += f"{rank}. **{member['user_name']}** — `{member['score']}` poin\n"
        rep = await message.reply_text(response)
        asyncio.create_task(auto_delete(message, 10)); asyncio.create_task(auto_delete(rep, 10))
    except Exception as e: print(f"❌ Error /typing_stats: {e}")

@app.on_message(filters.command("test_reset") & filters.group)
async def test_reset_command(client, message: Message):
    try:
        chat_id = message.chat.id
        if not await is_user_admin(chat_id, message.from_user.id if message.from_user else 0): return
        await message.reply_text("⏳ Memulai simulasi reset grup...")
        await dynamic_reset_job(chat_id)
    except Exception as e: print(f"❌ Error /test_reset: {e}")

# --- CALLBACK QUERY HANDLER (FIXED & MULTI-GROUP) ---
@app.on_callback_query()
async def handle_callbacks(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if user_id != OWNER_ID: return
    data = callback_query.data
    try: await callback_query.answer()
    except: pass

    try:
        if data == "none": return
        if data == "back_to_groups":
            USER_STATES.pop(user_id, None)
            cursor = config_collection.find()
            all_configs = await cursor.to_list(length=100)
            valid_groups = []
            for cfg in all_configs:
                cid = cfg.get("chat_id")
                if cid:
                    try:
                        chat = await client.get_chat(cid)
                        valid_groups.append({"chat_id": cid, "title": chat.title})
                    except: pass
            await callback_query.message.edit_text("Silakan pilih grup yang ingin Anda kelola:", reply_markup=group_selection_menu(valid_groups))
            return

        parts = data.split("_")
        chat_id = int(parts[-1]) if parts[-1].replace("-", "").isdigit() else None
        config = await get_config(chat_id) if chat_id else None

        if data.startswith("manage_"):
            USER_STATES.pop(user_id, None)
            await callback_query.message.edit_text(f"🛠️ **Control Panel**\n`Chat ID: {chat_id}`", reply_markup=main_owner_menu(chat_id))
        
        elif data.startswith("cfg_view_"):
            next_r = datetime.fromisoformat(config['next_reset']).strftime('%Y-%m-%d %H:%M:%S')
            if config['mode'] == "day": mode_text = f"Setiap `{config['reset_days']}` Hari"
            elif config['mode'] == "date": mode_text = f"Setiap Tanggal `{config['reset_date']}`"
            else: mode_text = f"Setiap Hari `{HARI_MAP.get(config.get('reset_weekday', 0))}`"
            
            p_text = ""
            for k, v in config.get("privileges", {}).items():
                if k in PRIVILEGE_LABELS:
                    p_text += f"• {PRIVILEGE_LABELS[k]}: {'✅ ON' if v else '❌ OFF'}\n"
                
            text = f"⚙️ **KONFIGURASI SEKARANG:**\n\n🔹 Mode: `{config['mode'].upper()}` ({mode_text})\n⏰ Waktu Reset: `{config['reset_hour']:02d}:{config['reset_minute']:02d} WIB`\n👑 Kuota Admin: Top `{config['max_admins']}` Teratas\n📅 Reset Berikutnya: `{next_r} WIB`\n\n🛡️ **Hak Kekuasaan Admin Baru:**\n{p_text}"
            await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali", callback_data=f"manage_{chat_id}")]]))
        
        elif data.startswith("cfg_mode_select_"):
            menu = InlineKeyboardMarkup([
                [InlineKeyboardButton("📆 Base Day (Per Berapa Hari)", callback_data=f"setmode_day_{chat_id}")],
                [InlineKeyboardButton("📅 Base Date (Per Tanggal Bulanan)", callback_data=f"setmode_date_{chat_id}")],
                [InlineKeyboardButton("🔙 Kembali", callback_data=f"manage_{chat_id}")]
            ])
            await callback_query.message.edit_text("⚙️ **Pilih Mode Penjadwalan Reset:**", reply_markup=menu)
            
        elif data.startswith("setmode_"):
            mode = parts[1]
            await config_collection.update_one({"chat_id": chat_id}, {"$set": {"mode": mode}})
            USER_STATES[user_id] = {"chat_id": chat_id, "action": f"input_{mode}"}
            if mode == "day":
                await callback_query.message.edit_text("✍️ **Sistem Siap!**\n\nKetik jumlah harinya saja, lalu diikuti jam dan menit.\nContoh ketik: `7:23:59` (Artinya reset per 7 hari sekali jam 23:59 WIB)")
            else:
                await callback_query.message.edit_text("✍️ **Sistem Siap!**\n\nKetik tanggalnya saja, lalu diikuti jam dan menit.\nContoh ketik: `1:00:00` (Artinya reset setiap tanggal 1 awal bulan jam 00:00 WIB)")

        elif data.startswith("cfg_max_admins_"):
            buttons = [[InlineKeyboardButton(f"{i} Admin", callback_data=f"setmax_{i}_{chat_id}")] for i in [1, 2, 3]]
            buttons.append([InlineKeyboardButton("🔙 Kembali", callback_data=f"manage_{chat_id}")])
            await callback_query.message.edit_text("👑 **Jumlah Admin Diangkat Bersamaan:**", reply_markup=InlineKeyboardMarkup(buttons))
            
        elif data.startswith("setmax_"):
            await config_collection.update_one({"chat_id": chat_id}, {"$set": {"max_admins": int(parts[1])}})
            await callback_query.message.edit_text(f"🛠️ **Control Panel**\n`Chat ID: {chat_id}`", reply_markup=main_owner_menu(chat_id))

        elif data.startswith("cfg_privs_"):
            await callback_query.message.edit_text("⚙️ **Atur hak akses kapasitas admin baru:**", reply_markup=privileges_menu(config['privileges'], chat_id))

        elif data.startswith("toggle_"):
            # Perbaikan extractor priv_key multi-grup agar aman dari bug underscore
            priv_key = data.replace("toggle_", "").replace(f"_{chat_id}", "")
            current_val = config['privileges'].get(priv_key, True)
            
            await config_collection.update_one({"chat_id": chat_id}, {"$set": {f"privileges.{priv_key}": not current_val}})
            
            config = await get_config(chat_id)
            await callback_query.message.edit_reply_markup(reply_markup=privileges_menu(config['privileges'], chat_id))
            await callback_query.answer("Hak akses diperbarui!")

    except Exception as e: print(f"💥 Crash Callback: {e}")

# --- INPUT TEKS OWNER ---
@app.on_message(filters.private & filters.text & ~filters.command(["start"]))
async def handle_smart_input(client, message: Message):
    user_id = message.from_user.id
    if user_id != OWNER_ID or user_id not in USER_STATES: return

    state = USER_STATES[user_id]
    chat_id = state["chat_id"]
    action = state["action"]
    text = message.text.strip()

    try:
        if ":" in text and len(text.split(":")) == 3:
            val1, hour, minute = map(int, text.split(":"))
            if action == "input_day":
                await config_collection.update_one({"chat_id": chat_id}, {"$set": {"reset_days": val1, "reset_hour": hour, "reset_minute": minute}})
            elif action == "input_date":
                await config_collection.update_one({"chat_id": chat_id}, {"$set": {"reset_date": val1, "reset_hour": hour, "reset_minute": minute}})

            updated_config = await get_config(chat_id)
            new_next_reset = calculate_next_reset(updated_config)
            await config_collection.update_one({"chat_id": chat_id}, {"$set": {"next_reset": new_next_reset}})
            
            USER_STATES.pop(user_id, None)
            await message.reply_text(f"✅ **Konfigurasi Berhasil Disimpan!**\nReset otomatis berikutnya pada: `{new_next_reset}` WIB", reply_markup=main_owner_menu(chat_id))
        else:
            await message.reply_text("❌ Format salah! Sesuai instruksi contoh di atas.")
    except Exception as e:
        print(f"💥 Error input: {e}")
        USER_STATES.pop(user_id, None)

# --- TRACK KEAKTIFAN MEMBER PER GRUP ---
@app.on_message(filters.group & ~filters.service)
async def track_messages(client, message: Message):
    try:
        chat_id = message.chat.id
        if message.text and message.text.startswith("/"): return
        if not message.from_user or message.from_user.is_bot: return
        user_id = message.from_user.id

        # Otomatis daftarkan grup baru ke DB jika belum terdaftar saat ada aktivitas chat
        await get_config(chat_id)

        await stats_collection.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$set": {"user_name": message.from_user.first_name or "User"}, "$inc": {"score": 1}},
            upsert=True
        )
    except Exception as e: print(f"❌ Error track_messages: {e}")

# --- PENGANGKATAN ADMIN ---
async def dynamic_reset_job(chat_id: int):
    try:
        config = await get_config(chat_id)
        max_admins = config.get("max_admins", 1)
        p = config.get('privileges', {})
        
        cursor = stats_collection.find({"chat_id": chat_id, "score": {"$gt": 0}}).sort("score", -1).limit(max_admins)
        winners = await cursor.to_list(length=max_admins)
        
        old_admins = await admin_collection.find({"chat_id": chat_id}).to_list(length=10)
        new_winner_ids = [w["user_id"] for w in winners] if winners else []
        for old in old_admins:
            if old["user_id"] not in new_winner_ids:
                try: await app.promote_chat_member(chat_id=chat_id, user_id=old["user_id"], privileges=ChatPrivileges(can_manage_chat=False))
                except: pass
        await admin_collection.delete_many({"chat_id": chat_id})

        announcement = "📢 **PERGANTIAN ADMIN OTOMATIS PERIODE BARU!** 📢\n\n"
        if winners:
            announcement += f"🏆 **Selamat kepada top {len(winners)} user teraktif!**\n\n"
            for idx, winner in enumerate(winners, start=1):
                uid = winner["user_id"]; uname = winner["user_name"]
                try:
                    await app.promote_chat_member(
                        chat_id=chat_id, user_id=uid,
                        privileges=ChatPrivileges(
                            can_manage_chat=True,
                            can_delete_messages=p.get('can_delete_messages', True),
                            can_restrict_members=p.get('can_restrict_members', True),
                            can_invite_users=p.get('can_invite_users', True),
                            can_pin_messages=p.get('can_pin_messages', True),
                            can_manage_video_chats=p.get('can_manage_video_chats', False)
                        )
                    )
                    try: await app.set_chat_administrator_custom_title(chat_id, uid, f"Top Member {idx} 👑")
                    except: pass
                    await admin_collection.insert_one({"chat_id": chat_id, "user_id": uid, "user_name": uname})
                    announcement += f"{idx}. [{uname}](tg://user?id={uid}) — `{winner['score']}` poin\n"
                except: announcement += f"{idx}. **{uname}** (⚠️ Gagal dipromosikan)\n"
        else: announcement += "Tidak ada aktivitas pesan periode ini. Posisi admin tetap. 🏝️"

        new_next_reset = calculate_next_reset(config)
        await config_collection.update_one({"chat_id": chat_id}, {"$set": {"next_reset": new_next_reset}})
        announcement += f"\n🔄 *Poin direset ke 0!*\n📅 Reset Berikutnya: `{new_next_reset} WIB`"
        try: await app.send_message(chat_id=chat_id, text=announcement)
        except: pass
        await stats_collection.delete_many({"chat_id": chat_id})
    except Exception as e: print(f"❌ Error dynamic_reset_job: {e}")

# TIME CHECKER WORKER
async def time_checker_loop():
    while True:
        try:
            cursor = config_collection.find()
            all_configs = await cursor.to_list(length=None)
            for config in all_configs:
                chat_id = config.get("chat_id")
                target_str = config.get("next_reset")
                if chat_id and target_str and datetime.now() >= datetime.fromisoformat(target_str):
                    await dynamic_reset_job(chat_id)
        except Exception as e: print(f"❌ Error checker loop: {e}")
        await asyncio.sleep(45)

async def main():
    try:
        await app.start()
        print("🤖 Bot Multi-Grup dengan Menu Pilihan Grup Siap!")
        asyncio.create_task(time_checker_loop())
        await idle()
        await app.stop()
    except Exception as e: print(f"💥 Engine Utama Crash: {e}")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
