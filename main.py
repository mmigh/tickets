import discord, asyncio, json, os
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from io import StringIO
from keep_alive import keep_alive

# ==== FILE PATHS ====
CONFIG_FILE = "config.json"
TICKET_FILE = "tickets.json"
BLACKLIST_FILE = "blacklist.json"

# ==== STORAGE CLASS ====
class Storage:
    def __init__(self, path, default):
        self.path = path
        self.data = self._load(default)
        self._dirty = False

    def _load(self, default):
        if not os.path.exists(self.path):
            return default
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def mark_dirty(self): self._dirty = True
    def save_now(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=4)
        self._dirty = False

# ==== INIT STORAGES ====
config = Storage(CONFIG_FILE, {})
tickets = Storage(TICKET_FILE, {"last_id": 0, "tickets": {}})
blacklist = Storage(BLACKLIST_FILE, {"users": [], "roles": []})

# ==== PERIODIC SAVE TASK ====
async def periodic_saver():
    while True:
        await asyncio.sleep(5)
        for s in (config, tickets, blacklist):
            if s._dirty:
                try:
                    s.save_now()
                except Exception as e:
                    print("Save error:", e)

# ==== HELPERS ====
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

def is_blacklisted(guild, user):
    if user.id in blacklist.data.get("users", []): return True
    if any(r.id in blacklist.data.get("roles", []) for r in user.roles): return True
    return False

async def ensure_logs_channel(guild):
    gid = str(guild.id)
    gconf = config.data.get(gid, {})
    if gconf.get("log_channel"):
        ch = guild.get_channel(gconf["log_channel"])
        if ch: return ch
    existing = discord.utils.get(guild.text_channels, name="logs-ticket")
    if existing: return existing
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True)
    }
    return await guild.create_text_channel("logs-ticket", overwrites=overwrites)

async def generate_transcript(channel: discord.TextChannel) -> discord.File:
    buf = StringIO()
    buf.write("<html><head><meta charset='utf-8'><title>Transcript</title></head><body>")
    buf.write(f"<h2>Transcript of {channel.name}</h2><hr>")
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = m.clean_content.replace('\n', '<br>')
        buf.write(f"<p><b>[{ts}] {m.author}:</b> {content}</p>")
        for a in m.attachments:
            buf.write(f"<p>📎 <a href='{a.url}'>{a.filename}</a></p>")
    buf.write("</body></html>")
    buf.seek(0)
    return discord.File(buf, filename=f"{channel.name}-transcript.html")

# ==== PERMISSIONS ====
def is_admin_or_staff():
    async def predicate(interaction: discord.Interaction):
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
    return app_commands.check(predicate)

def is_admin_or_staff_or_owner():
    async def predicate(interaction: discord.Interaction):
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
        if cid in tickets.data["tickets"] and tickets.data["tickets"][cid]["user"] == interaction.user.id:
            return True
        raise app_commands.CheckFailure("❌ Bạn không có quyền dùng lệnh này.")
    return app_commands.check(predicate)

# ==== BOT SETUP ====
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ==== VIEW CREATOR ====
def make_ticket_view(guild_id: int):
    gid = str(guild_id)
    gconf = config.data.get(gid, {})
    view = discord.ui.View(timeout=None)

    standard = [("🛒 Mua hàng", "Mua hàng"), ("⚡ Cày thuê", "Cày thuê"),
                ("🛠️ Báo lỗi", "Báo lỗi"), ("📩 Khác", "Khác")]
    custom = gconf.get("custom_buttons", [])

    async def create_ticket(interaction, ticket_type):
        await interaction.response.defer(ephemeral=True)
        await create_ticket_from_interaction(interaction, ticket_type)

    for label, ttype in standard:
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
        btn.callback = lambda i, t=ttype: asyncio.create_task(create_ticket(i, t))
        view.add_item(btn)

    for label in custom:
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        btn.callback = lambda i, t=label: asyncio.create_task(create_ticket(i, t))
        view.add_item(btn)

    return view

# ==== CREATE TICKET ====
async def create_ticket_from_interaction(interaction, ticket_type):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    category_id = gconf.get("ticket_category")
    category = interaction.guild.get_channel(category_id)
    if not category:
        return await interaction.followup.send("❌ Ticket system chưa setup!", ephemeral=True)
    if is_blacklisted(interaction.guild, interaction.user):
        return await interaction.followup.send("🚫 Bạn đã bị blacklist!", ephemeral=True)

    tickets.data["last_id"] += 1
    tid = tickets.data["last_id"]
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True)
    }
    staff_role_id = gconf.get("staff_role")
    if staff_role_id:
        staff = interaction.guild.get_role(staff_role_id)
        if staff:
            overwrites[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    ch_name = f"ticket-{tid}-{ticket_type.replace(' ', '').lower()}"
    ch = await category.create_text_channel(name=ch_name, overwrites=overwrites)
    tickets.data["tickets"][str(ch.id)] = {"id": tid, "user": interaction.user.id, "type": ticket_type}
    tickets.mark_dirty()

    await ch.send(f"🎟️ Ticket #{tid} | {ticket_type} – Xin chào {interaction.user.mention}!")
    await interaction.followup.send(f"✅ Ticket #{tid} đã được tạo: {ch.mention}", ephemeral=True)

    log_ch = await ensure_logs_channel(interaction.guild)
    await log_ch.send(f"🟢 Ticket #{tid} mở bởi {interaction.user.mention}")

# ==== COMMANDS ====

@bot.tree.command(name="setup", description="Cấu hình hệ thống ticket")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction, category: discord.CategoryChannel, staff_role: discord.Role, log_channel: discord.TextChannel):
    gid = str(interaction.guild.id)
    config.data[gid] = {
        "ticket_category": category.id,
        "staff_role": staff_role.id,
        "log_channel": log_channel.id,
        "custom_buttons": config.data.get(gid, {}).get("custom_buttons", []),
        "panel_message": None
    }
    config.mark_dirty()
    await interaction.response.send_message("✅ Setup hoàn tất!", ephemeral=True)

@bot.tree.command(name="panel", description="Gửi panel mở ticket")
@app_commands.checks.has_permissions(administrator=True)
async def panel(interaction):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    embed = discord.Embed(
        title="⚡ Open Ticket – Giải quyết nhanh chóng",
        description=(
            "Xin chào 👋\n"
            "Nếu bạn gặp vấn đề hoặc cần hỗ trợ, vui lòng mở ticket bằng cách chọn loại hỗ trợ bên dưới.\n\n"
            "⚡ **Danh mục hỗ trợ:**\n"
            "🛒 Mua hàng\n"
            "⚡ Cày thuê\n"
            "🛠️ Báo lỗi\n"
            "📩 Khác\n\n"
            "❌ **Lưu ý:**\n"
            "• Ghi rõ thông tin để được hỗ trợ nhanh chóng.\n"
            "• Không spam hoặc mở nhiều ticket cùng lúc.\n"
            "• Admin/Support sẽ phản hồi sớm nhất có thể.\n\n👉 Chọn **nút bên dưới** để bắt đầu!"
        ),
        color=discord.Color.blue()
    )
    view = make_ticket_view(interaction.guild.id)
    msg = await interaction.channel.send(embed=embed, view=view)
    config.data[gid]["panel_message"] = {"channel": interaction.channel.id, "message": msg.id}
    config.mark_dirty()
    await interaction.response.send_message("✅ Panel đã được gửi!", ephemeral=True)

@bot.tree.command(name="add_button", description="Thêm nút custom vào panel")
@is_admin_or_staff()
async def add_button(interaction, names: str):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    parts = [p.strip() for p in names.split() if p.strip()]
    added = []
    for name in parts:
        if name not in config.data[gid]["custom_buttons"]:
            config.data[gid]["custom_buttons"].append(name)
            added.append(name)
    if added:
        config.mark_dirty()
        bot.add_view(make_ticket_view(interaction.guild.id))
        await interaction.response.send_message(f"✅ Đã thêm: {', '.join(added)}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Không có nút mới.", ephemeral=True)

@bot.tree.command(name="remove_button", description="Xoá nút custom")
@is_admin_or_staff()
async def remove_button(interaction, name: str):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    if name in config.data[gid]["custom_buttons"]:
        config.data[gid]["custom_buttons"].remove(name)
        config.mark_dirty()
        await interaction.response.send_message(f"✅ Đã xoá nút **{name}**", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Không tìm thấy nút.", ephemeral=True)

@bot.tree.command(name="close", description="Đóng ticket hiện tại và gửi transcript")
@is_admin_or_staff_or_owner()
async def close(interaction):
    cid = str(interaction.channel.id)
    if cid not in tickets.data["tickets"]:
        return await interaction.response.send_message("❌ Không phải ticket!", ephemeral=True)
    info = tickets.data["tickets"].pop(cid)
    tickets.mark_dirty()
    transcript = await generate_transcript(interaction.channel)
    log_ch = await ensure_logs_channel(interaction.guild)
    await log_ch.send(f"🔴 Ticket #{info['id']} đóng bởi {interaction.user.mention}", file=transcript)
    await interaction.response.send_message("✅ Ticket đã đóng!", ephemeral=True)
    await asyncio.sleep(3)
    await interaction.channel.delete()

@bot.tree.command(name="refresh_panel", description="Làm mới panel ticket ngay lập tức")
@is_admin_or_staff()
async def refresh_panel(interaction):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    embed = discord.Embed(
        title="⚡ Open Ticket – Giải quyết nhanh chóng",
        description=(
            "Xin chào 👋\n"
            "Nếu bạn gặp vấn đề hoặc cần hỗ trợ, vui lòng mở ticket bằng cách chọn loại hỗ trợ bên dưới.\n\n"
            "⚡ **Danh mục hỗ trợ:**\n"
            "🛒 Mua hàng\n"
            "⚡ Cày thuê\n"
            "🛠️ Báo lỗi\n"
            "📩 Khác\n\n"
            "❌ **Lưu ý:**\n"
            "• Ghi rõ thông tin để được hỗ trợ nhanh chóng.\n"
            "• Không spam hoặc mở nhiều ticket cùng lúc.\n"
            "• Admin/Support sẽ phản hồi sớm nhất có thể.\n\n👉 Chọn **nút bên dưới** để bắt đầu!"
        ),
        color=discord.Color.blue()
    )
    view = make_ticket_view(interaction.guild.id)
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Panel đã được làm mới!", ephemeral=True)

# ==== READY ====
@bot.event
async def on_ready():
    bot.loop.create_task(periodic_saver())
    for gid in config.data.keys():
        try:
            bot.add_view(make_ticket_view(int(gid)))
        except Exception:
            pass
    print(f"✅ Logged in as {bot.user}")
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Sync failed:", e)

# ==== RUN ====
if __name__ == "__main__":
    keep_alive()
    load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in environment (.env)")
bot.run(TOKEN)
