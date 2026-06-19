import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters
from pyrogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Load variabel dari file .env
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("MONGO_DB_NAME")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))

# Inisialisasi Database MongoDB (menggunakan Motor untuk Async)
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]
stats_collection = db["typing_stats"]

# Inisialisasi Pyrogram Client
app = Client("typing_classifier_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Fungsi untuk mendapatkan klasifikasi RPG 10 Tingkat
def get_rpg_classification(score):
    if score >= 300: return "🌌 God Level"
    elif score >= 200: return "👑 Mythic Grandmaster"
    elif score >= 150: return "🐉 Dragon Slayer"
    elif score >= 100: return "🔮 Platinum Sage"
    elif score >= 75: return "🦅 Gold Knight"
    elif score >= 50: return "🏹 Silver Vanguard"
    elif score >= 30: return "🛡️ Iron Warrior"
    elif score >= 15: return "⚔️ Bronze Adventurer"
    elif score >= 5: return "🌾 Novice"
    else: return "🪵 Vagabond"

# 1. Handler Mendeteksi Sinyal Typing
@app.on_chat_action()
async def track_typing(client, chat_action):
    if chat_action.action == "typing":
        user = chat_action.from_user
        if not user or user.is_bot:
            return
        
        user_id = user.id
        user_name = user.first_name

        # Update atau Insert data ke MongoDB (Increment score +1)
        await stats_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {"user_name": user_name},
                "$inc": {"score": 1}
            },
            upsert=True
        )

# 2. Command Manual untuk Cek Leaderboard & Rank Saat Ini
@app.on_message(filters.command("typing_stats") & filters.group)
async def check_stats(client, message: Message):
    # Ambil top 10 member dari MongoDB urut berdasarkan skor tertinggi
    cursor = stats_collection.find().sort("score", -1).limit(10)
    top_members = await cursor.to_list(length=10)
    
    if not top_members:
        await message.reply_text("📭 Belum ada data mengetik yang terekam minggu ini.")
        return
    
    response = "📊 **PAPAN PERINGKAT TYPING MINGGU INI:**\n\n"
    for rank, member in enumerate(top_members, start=1):
        title = get_rpg_classification(member["score"])
        response += f"{rank}. **{member['user_name']}**\n"
        response += f"   Pangkat: {title} ({member['score']} poin)\n\n"
        
    await message.reply_text(response)

# 3. Fungsi Otomatis: Pengumuman Juara & Reset Mingguan
async def weekly_reset_job():
    cursor = stats_collection.find().sort("score", -1).limit(3)
    top_three = await cursor.to_list(length=3)
    
    announcement = "📢 **PENGUMUMAN JUARA TYPING MINGGUAN!** 📢\n"
    announcement += "Selamat kepada para petualang yang paling aktif mengetik minggu ini:\n\n"
    
    if top_three:
        medals = ["🥇", "🥈", "🥉"]
        for idx, member in enumerate(top_three):
            title = get_rpg_classification(member["score"])
            announcement += f"{medals[idx]} **{member['user_name']}** - {title} ({member['score']} poin)\n"
    else:
        announcement += "Sayang sekali, tidak ada aktivitas mengetik minggu ini. 🏝️"
        
    announcement += "\n🔄 *Poin telah direset ke 0 untuk minggu yang baru! Pangkat Anda kembali menjadi Vagabond.*"
    
    # Kirim pengumuman ke grup
    try:
        await app.send_message(chat_id=GROUP_CHAT_ID, text=announcement)
    except Exception as e:
        print(f"Gagal mengirim pengumuman: {e}")
        
    # Hapus/Reset semua skor di MongoDB
    await stats_collection.delete_many({})
    print("Database berhasil direset untuk minggu baru.")

# Mengatur Scheduler (Berjalan setiap hari Minggu jam 23:59)
scheduler = AsyncIOScheduler(timezone="Asia/Jakarta")
scheduler.add_job(weekly_reset_job, 'cron', day_of_week='sun', hour=23, minute=59)

# Fungsi utama untuk menjalankan Bot dan Scheduler bersamaan
async def main():
    scheduler.start()
    print("⏰ Scheduler Reset Mingguan aktif (Setiap Minggu 23:59 WIB)...")
    
    print("🤖 Bot Pyrogram berjalan...")
    await app.start()
    
    # Menjaga agar script tetap hidup (idle)
    await pyrogram.methods.utilities.idle.idle()
    
    # Stop bot jika dimatikan
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
