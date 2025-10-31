# =====================================================
# Discord Ticket Bot — Full Version + UptimeRobot Support
# =====================================================

import discord, asyncio, os, json, threading
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from io import StringIO
from flask import Flask

# ================= FILES =================
CONFIG_FILE = "config.json"
TICKET_FILE = "tickets.json"
BLACKLIST_FILE = "blacklist.json"


# ================= STORAGE =================
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


config = Storage(CONFIG_FILE, {})
tickets = Storage(TICKET_FILE, {"last_id": 0, "tickets": {}})
blacklist = Storage(BLACKLIST_FILE, {"users": [], "roles": []})


# ================= AUTO SAVE =================
async def periodic_saver():
    while True:
        await asyncio.sleep(5)
        for s in (config, tickets, blacklist):
            if getattr(s, "_dirty", False):
                try:
                    s.save_now()
                except Exception as e:
                    print("Save failed:", e)


# ================= HELPERS =================
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


async def ensure_logs_channel(guild: discord.Guild):
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
    ch = await guild.create_text_channel("logs-ticket", overwrites=overwrites)
    return ch


async def log_ticket_event(guild, message):
    try:
        ch = await ensure_logs_channel(guild)
        await ch.send(message)
    except Exception as e:
        print("Failed to log:", e)


async def generate_transcript(channel):
    buf = StringIO()
    buf.write(f"<html><meta charset='utf-8'><body>")
    buf.write(f"<h2>Transcript: {channel.name}</h2><hr>")
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        buf.write(f"<p><b>[{ts}] {m.author}:</b> {m.clean_content}</p>")
    buf.write("</body></html>")
    buf.seek(0)
    return discord.File(buf, f"{channel.name}-transcript.html")


# ================= PERMISSIONS =================
def is_admin_or_staff():
    async def pred(inter: discord.Interaction):
        gid = str(inter.guild.id)
        gconf = config.data.get(gid, {})
        if inter.user.guild_permissions.administrator:
            return True
        staff_role = gconf.get("staff_role")
        if staff_role:
            r = inter.guild.get_role(staff_role)
            if r and r in inter.user.roles:
                return True
        raise app_commands.CheckFailure("❌ Bạn không có quyền dùng lệnh này.")
    return app_commands.check(pred)


def is_admin_or_staff_or_owner():
    async def pred(inter: discord.Interaction):
        gid = str(inter.guild.id)
        gconf = config.data.get(gid, {})
        if inter.user.guild_permissions.administrator:
            return True
        staff_role = gconf.get("staff_role")
        if staff_role:
            r = inter.guild.get_role(staff_role)
            if r and r in inter.user.roles:
                return True
        cid = str(inter.channel.id)
        if cid in tickets.data["tickets"]:
            if tickets.data["tickets"][cid]["user"] == inter.user.id:
                return True
        raise app_commands.CheckFailure("❌ Bạn không có quyền dùng lệnh này.")
    return app_commands.check(pred)


# ================= BOT =================
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)


# ================= MAKE BUTTONS =================
def make_ticket_view(guild_id: int):
    gid = str(guild_id)
    gconf = config.data.get(gid, {})
    view = discord.ui.View(timeout=None)
    custom = gconf.get("custom_buttons", [])
    standard = [("🛒 Mua hàng", "Mua hàng"),
                ("⚡ Cày thuê", "Cày thuê"),
                ("🛠️ Báo lỗi", "Báo lỗi"),
                ("📩 Khác", "Khác")]

    def make_cb(ttype):
        async def cb(inter):
            await inter.response.defer(ephemeral=True)
            await create_ticket_from_interaction(inter, ttype)
        return cb

    for i, (label, ttype) in enumerate(standard):
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"std_{gid}_{i}")
        btn.callback = make_cb(ttype)
        view.add_item(btn)

    for i, lbl in enumerate(custom):
        btn = discord.ui.Button(label=lbl, style=discord.ButtonStyle.primary, custom_id=f"cus_{gid}_{i}")
        async def cb(inter, t=lbl):
            await inter.response.defer(ephemeral=True)
            await create_ticket_from_interaction(inter, t)
        btn.callback = cb
        view.add_item(btn)

    return view


# ================= CREATE TICKET =================
async def create_ticket_from_interaction(inter, ttype):
    gid = str(inter.guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    category = inter.guild.get_channel(gconf.get("ticket_category"))
    if not category:
        return await inter.followup.send("❌ Ticket chưa setup!", ephemeral=True)
    if is_blacklisted(inter.guild, inter.user):
        return await inter.followup.send("🚫 Bạn đã bị blacklist!", ephemeral=True)
    tickets.data["last_id"] += 1
    tid = tickets.data["last_id"]

    overwrites = {
        inter.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        inter.user: discord.PermissionOverwrite(view_channel=True)
    }
    staff = inter.guild.get_role(gconf.get("staff_role")) if gconf.get("staff_role") else None
    if staff:
        overwrites[staff] = discord.PermissionOverwrite(view_channel=True)
    ch = await category.create_text_channel(f"ticket-{tid}-{ttype.replace(' ','')}", overwrites=overwrites)
    tickets.data["tickets"][str(ch.id)] = {"id": tid, "user": inter.user.id, "type": ttype}
    tickets.mark_dirty()
    await inter.followup.send(f"✅ Ticket #{tid} ({ttype}) đã tạo: {ch.mention}", ephemeral=True)
    await ch.send(f"🎫 Ticket #{tid} — {ttype}\nXin chào {inter.user.mention}!")
    await log_ticket_event(inter.guild, f"🟢 Ticket #{tid} tạo bởi {inter.user.mention}")


# ================= COMMANDS =================
@bot.tree.command(name="setup_ticket", description="Cấu hình ticket system")
@is_admin_or_staff()
async def setup_ticket(inter, category: discord.CategoryChannel, staff_role: discord.Role, log_channel: discord.TextChannel):
    gid = str(inter.guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    gconf["ticket_category"] = category.id
    gconf["staff_role"] = staff_role.id
    gconf["log_channel"] = log_channel.id
    config.mark_dirty()
    await inter.response.send_message("✅ Setup thành công!", ephemeral=True)


@bot.tree.command(name="panel", description="Gửi bảng tạo ticket")
@is_admin_or_staff()
async def panel(inter):
    gid = str(inter.guild.id)
    ensure_guild_config(gid)
    view = make_ticket_view(inter.guild.id)
    msg = await inter.channel.send("🎫 **Tạo ticket tại đây:**", view=view)
    config.data[gid]["panel_message"] = msg.id
    config.mark_dirty()
    await inter.response.send_message("✅ Đã gửi panel!", ephemeral=True)


@bot.tree.command(name="close", description="Đóng ticket")
@is_admin_or_staff_or_owner()
async def close(inter):
    cid = str(inter.channel.id)
    if cid not in tickets.data["tickets"]:
        return await inter.response.send_message("Không phải kênh ticket.", ephemeral=True)
    file = await generate_transcript(inter.channel)
    user_id = tickets.data["tickets"][cid]["user"]
    user = inter.guild.get_member(user_id)
    if user:
        try:
            await user.send("📜 Ticket của bạn đã đóng:", file=file)
        except:
            pass
    del tickets.data["tickets"][cid]
    tickets.mark_dirty()
    await log_ticket_event(inter.guild, f"🔴 Ticket {cid} đóng bởi {inter.user.mention}")
    await inter.response.send_message("✅ Đóng ticket sau 5s.")
    await asyncio.sleep(5)
    await inter.channel.delete()


@bot.tree.command(name="rename", description="Đổi tên ticket")
@is_admin_or_staff_or_owner()
async def rename(inter, new_name: str):
    await inter.channel.edit(name=new_name)
    await inter.response.send_message(f"✅ Đã đổi tên kênh thành `{new_name}`", ephemeral=True)


@bot.tree.command(name="add", description="Thêm người vào ticket")
@is_admin_or_staff_or_owner()
async def add(inter, member: discord.Member):
    await inter.channel.set_permissions(member, view_channel=True, send_messages=True)
    await inter.response.send_message(f"✅ Đã thêm {member.mention} vào ticket!", ephemeral=True)


@bot.tree.command(name="blacklist", description="Thêm vào blacklist")
@is_admin_or_staff()
async def blacklist_cmd(inter, member: discord.Member = None, role: discord.Role = None):
    if member:
        blacklist.data["users"].append(member.id)
    elif role:
        blacklist.data["roles"].append(role.id)
    else:
        return await inter.response.send_message("❌ Hãy chọn user hoặc role!", ephemeral=True)
    blacklist.mark_dirty()
    await inter.response.send_message("✅ Đã thêm vào blacklist.", ephemeral=True)


@bot.tree.command(name="unblacklist", description="Gỡ khỏi blacklist")
@is_admin_or_staff()
async def unblacklist(inter, member: discord.Member = None, role: discord.Role = None):
    if member and member.id in blacklist.data["users"]:
        blacklist.data["users"].remove(member.id)
    elif role and role.id in blacklist.data["roles"]:
        blacklist.data["roles"].remove(role.id)
    else:
        return await inter.response.send_message("❌ Không tìm thấy!", ephemeral=True)
    blacklist.mark_dirty()
    await inter.response.send_message("✅ Đã gỡ khỏi blacklist.", ephemeral=True)


@bot.tree.command(name="button", description="Thêm nút custom")
@is_admin_or_staff()
async def button_cmd(inter, label: str):
    gid = str(inter.guild.id)
    ensure_guild_config(gid)
    config.data[gid]["custom_buttons"].append(label)
    config.mark_dirty()
    await inter.response.send_message(f"✅ Đã thêm nút `{label}`!", ephemeral=True)


@bot.tree.command(name="re_sync", description="Đồng bộ lại command")
@is_admin_or_staff()
async def re_sync(inter):
    await bot.tree.sync()
    await inter.response.send_message("✅ Đã sync lại slash commands!", ephemeral=True)


# ================= STARTUP =================
@bot.event
async def on_ready():
    bot.loop.create_task(periodic_saver())
    for gid in config.data.keys():
        try:
            bot.add_view(make_ticket_view(int(gid)))
        except:
            pass
    print(f"✅ Logged in as {bot.user}")
    try:
        await bot.tree.sync()
        print("✅ Commands synced!")
    except Exception as e:
        print("Sync failed:", e)


# ================= KEEP ALIVE (UptimeRobot) =================
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Ticket bot is running!"

def run_web():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web).start()


# ================= RUN =================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")
bot.run(TOKEN)
