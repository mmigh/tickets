import discord
from discord.ext import commands
from discord import app_commands
import json, os, asyncio
from dotenv import load_dotenv
from io import StringIO
from typing import List
from flask import Flask
import threading

# ====== FILE PATHS ======
CONFIG_FILE = "config.json"
TICKET_FILE = "tickets.json"
BLACKLIST_FILE = "blacklist.json"

# ====== STORAGE (sync-to-disk debounced) ======
class Storage:
    def __init__(self, path, default):
        self.path = path
        self.data = self._load(default)
        self._dirty = False

    def _load(self, default):
        if not os.path.exists(self.path):
            return default
        with open(self.path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return default

    def mark_dirty(self):
        self._dirty = True

    def save_now(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=4)
        self._dirty = False

# Instantiate storages
config = Storage(CONFIG_FILE, {})
tickets = Storage(TICKET_FILE, {"last_id": 0, "tickets": {}})
blacklist = Storage(BLACKLIST_FILE, {"users": [], "roles": []})

# ====== BACKGROUND SAVE TASK ======
async def periodic_saver():
    while True:
        await asyncio.sleep(5)
        for s in (config, tickets, blacklist):
            if getattr(s, "_dirty", False):
                try:
                    s.save_now()
                except Exception as e:
                    print("Save failed:", e)

# ====== HELPERS ======
def ensure_guild_config(gid: str):
    if gid not in config.data:
        config.data[gid] = {
            "ticket_category": None,
            "staff_role": None,
            "log_channel": None,
            "custom_buttons": [],
            "panel_message": None
        }
        config.mark_dirty()

def is_blacklisted(guild: discord.Guild, user: discord.Member):
    if user.id in blacklist.data.get("users", []):
        return "user"
    if any(r.id in blacklist.data.get("roles", []) for r in user.roles):
        return "role"
    return None

async def ensure_logs_channel(guild: discord.Guild) -> discord.TextChannel:
    gid = str(guild.id)
    gconf = config.data.get(gid, {})
    if gconf.get("log_channel"):
        ch = guild.get_channel(gconf["log_channel"])
        if ch:
            return ch
    existing = discord.utils.get(guild.text_channels, name="logs-ticket")
    if existing:
        return existing
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True)
    }
    return await guild.create_text_channel("logs-ticket", overwrites=overwrites)

async def log_ticket_event(guild: discord.Guild, message: str):
    try:
        ch = await ensure_logs_channel(guild)
        await ch.send(message)
    except Exception as e:
        print("Failed to log event:", e)

async def generate_transcript(channel: discord.TextChannel) -> discord.File:
    buf = StringIO()
    buf.write(f"<html><head><meta charset='utf-8'><title>{channel.name}</title></head><body>")
    buf.write(f"<h2>Transcript of {channel.name}</h2><hr>\n")
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{m.author} ({m.author.id})"
        content = m.clean_content.replace('\n', '<br>')
        buf.write(f"<p><b>[{ts}] {author}:</b> {content}</p>\n")
        for a in m.attachments:
            buf.write(f"<p>📎 <a href='{a.url}'>{a.filename}</a></p>\n")
    buf.write("</body></html>")
    buf.seek(0)
    return discord.File(fp=buf, filename=f"{channel.name}-transcript.html")

# ====== PERMISSIONS ======
def is_admin_or_staff():
    async def pred(interaction: discord.Interaction):
        gid = str(interaction.guild.id)
        gconf = config.data.get(gid, {})
        if interaction.user.guild_permissions.administrator:
            return True
        staff_role_id = gconf.get("staff_role")
        if staff_role_id:
            role = interaction.guild.get_role(staff_role_id)
            if role and role in interaction.user.roles:
                return True
        raise app_commands.CheckFailure("❌ Bạn không có quyền dùng lệnh này.")
    return app_commands.check(pred)

def is_admin_or_staff_or_owner():
    async def pred(interaction: discord.Interaction):
        gid = str(interaction.guild.id)
        gconf = config.data.get(gid, {})
        if interaction.user.guild_permissions.administrator:
            return True
        staff_role_id = gconf.get("staff_role")
        if staff_role_id:
            role = interaction.guild.get_role(staff_role_id)
            if role and role in interaction.user.roles:
                return True
        cid = str(interaction.channel.id)
        if cid in tickets.data.get("tickets", {}):
            if tickets.data["tickets"][cid]["user"] == interaction.user.id:
                return True
        raise app_commands.CheckFailure("❌ Bạn không có quyền dùng lệnh này.")
    return app_commands.check(pred)

# ====== BOT SETUP ======
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ====== MAKE TICKET VIEW ======
def make_ticket_view(guild_id: int) -> discord.ui.View:
    gid = str(guild_id)
    gconf = config.data.get(gid, {})
    custom = gconf.get("custom_buttons", []) if gconf else []
    view = discord.ui.View(timeout=None)
    standard = [("🛒 Mua hàng", "Mua hàng"), ("⚡ Cày thuê", "Cày thuê"),
                ("🛠️ Báo lỗi", "Báo lỗi"), ("📩 Khác", "Khác")]

    def make_cb(ttype):
        async def cb(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await create_ticket_from_interaction(interaction, ttype)
        return cb

    for idx, (label, ttype) in enumerate(standard):
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"std_{gid}_{idx}")
        btn.callback = make_cb(ttype)
        view.add_item(btn)

    for cidx, label in enumerate(custom):
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=f"custom_{gid}_{cidx}")
        btn.callback = make_cb(label)
        view.add_item(btn)
    return view

# ====== CREATE TICKET ======
async def create_ticket_from_interaction(interaction: discord.Interaction, ticket_type: str):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data.get(gid, {})
    category_id = gconf.get("ticket_category")
    category = interaction.guild.get_channel(category_id) if category_id else None
    if not category:
        return await interaction.followup.send("❌ Ticket system chưa được setup (category missing).", ephemeral=True)
    reason = is_blacklisted(interaction.guild, interaction.user)
    if reason:
        return await interaction.followup.send(f"🚫 Bạn đã bị blacklist theo {reason}!", ephemeral=True)
    tickets.data["last_id"] += 1
    tid = tickets.data["last_id"]
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True)
    }
    staff_role_id = gconf.get("staff_role")
    if staff_role_id:
        staff_role = interaction.guild.get_role(staff_role_id)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    ch = await category.create_text_channel(f"ticket-{tid}-{ticket_type.replace(' ', '').lower()}", overwrites=overwrites)
    tickets.data["tickets"][str(ch.id)] = {"id": tid, "user": interaction.user.id, "type": ticket_type}
    tickets.mark_dirty()
    await interaction.followup.send(f"✅ Ticket #{tid} (**{ticket_type}**) đã được tạo: {ch.mention}", ephemeral=True)
    await ch.send(f"🎟️ Ticket #{tid} | {ticket_type} – Xin chào {interaction.user.mention}!")
    await log_ticket_event(interaction.guild, f"🟢 Ticket #{tid} | created by {interaction.user.mention}")

# ====== COMMANDS ======
@bot.tree.command(name="setup_ticket", description="Setup hệ thống ticket (category, staff role, log channel)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_ticket(interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role, log_channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild.id)
    config.data[gid] = {
        "ticket_category": category.id,
        "staff_role": staff_role.id,
        "log_channel": log_channel.id,
        "custom_buttons": config.data.get(gid, {}).get("custom_buttons", []),
        "panel_message": config.data.get(gid, {}).get("panel_message")
    }
    config.mark_dirty()
    await interaction.followup.send(f"✅ Ticket system setup!\nCategory: {category.mention}\nStaff: {staff_role.mention}\nLog: {log_channel.mention}", ephemeral=True)
    await log_ticket_event(interaction.guild, f"⚙️ Ticket system setup by {interaction.user.mention}")

@bot.tree.command(name="panel", description="Gửi panel ticket")
@app_commands.checks.has_permissions(administrator=True)
async def panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data.get(gid, {})
    if not gconf.get("ticket_category") or not gconf.get("staff_role"):
        return await interaction.followup.send("❌ Hệ thống ticket chưa setup! Dùng `/setup_ticket` trước.", ephemeral=True)
    embed = discord.Embed(
        title="⚡ Open Ticket – Giải quyết nhanh chóng",
        description=(
            "Xin chào 👋\nNếu bạn gặp vấn đề hoặc cần hỗ trợ, vui lòng mở ticket bằng cách chọn loại hỗ trợ bên dưới.\n\n"
            "⚡ **Danh mục hỗ trợ:**\n🛒 Mua hàng\n⚡ Cày thuê\n🛠️ Báo lỗi\n📩 Khác\n\n"
            "❌ **Lưu ý:**\n• Ghi rõ thông tin để được hỗ trợ nhanh chóng.\n"
            "• Không spam hoặc mở nhiều ticket cùng lúc.\n• Admin/Support sẽ phản hồi sớm nhất có thể.\n\n👉 Chọn **nút bên dưới** để bắt đầu!"
        ),
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/561/561127.png")
    embed.set_footer(text="Ticket System")
    view = make_ticket_view(interaction.guild.id)
    msg = await interaction.channel.send(embed=embed, view=view)
    config.data[gid]["panel_message"] = {"channel": interaction.channel.id, "message": msg.id}
    config.mark_dirty()
    await interaction.followup.send("✅ Panel đã được gửi!", ephemeral=True)
    await log_ticket_event(interaction.guild, f"🟢 {interaction.user.mention} gửi panel ticket tại {interaction.channel.mention}")

@bot.tree.command(name="close", description="Đóng ticket và gửi transcript")
@is_admin_or_staff_or_owner()
async def close(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cid = str(interaction.channel.id)
    if cid not in tickets.data.get("tickets", {}):
        return await interaction.followup.send("❌ Đây không phải ticket!", ephemeral=True)
    info = tickets.data["tickets"].pop(cid)
    tickets.mark_dirty()
    transcript = await generate_transcript(interaction.channel)
    logs = await ensure_logs_channel(interaction.guild)
    await logs.send(content=f"🔴 Ticket #{info['id']} | closed by {interaction.user.mention}", file=transcript)
    await interaction.followup.send("✅ Ticket đã được đóng và transcript đã gửi về logs channel!", ephemeral=True)
    await asyncio.sleep(3)
    await interaction.channel.delete()

@bot.tree.command(name="add", description="Thêm người dùng vào ticket hiện tại")
@is_admin_or_staff_or_owner()
async def add(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    cid = str(interaction.channel.id)
    if cid not in tickets.data["tickets"]:
        return await interaction.followup.send("❌ Đây không phải ticket!", ephemeral=True)
    await interaction.channel.set_permissions(member, view_channel=True, send_messages=True)
    await interaction.followup.send(f"✅ Đã thêm {member.mention} vào ticket!", ephemeral=True)
    await log_ticket_event(interaction.guild, f"👤 {member.mention} added by {interaction.user.mention}")

# ====== FLASK KEEP ALIVE ======
app = Flask(__name__)

@app.route('/')
def home():
    return "Ticket Bot is alive!"

def run_web():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web).start()

# ====== BOT READY ======
@bot.event
async def on_ready():
    bot.loop.create_task(periodic_saver())
    for gid, gconf in config.data.items():
        try:
            bot.add_view(make_ticket_view(int(gid)))
        except Exception:
            pass
    print(f"✅ Logged in as {bot.user} — {len(config.data)} guilds loaded")
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Failed to sync commands:", e)

# ====== RUN ======
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in environment (.env)")
bot.run(TOKEN)
