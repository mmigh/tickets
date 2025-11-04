# tickets.py — Optimized, persistent buttons, /button and /re_sync added
import discord
from discord.ext import commands, tasks
from discord import app_commands
import json, os, asyncio
from dotenv import load_dotenv
from io import StringIO
from typing import List
from keep_alive import keep_alive

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

# Instantiate storages (in-memory)
config = Storage(CONFIG_FILE, {})
tickets = Storage(TICKET_FILE, {"last_id": 0, "tickets": {}})
blacklist = Storage(BLACKLIST_FILE, {"users": [], "roles": []})

# Periodic background save task will flush dirty storages
async def periodic_saver():
    while True:
        await asyncio.sleep(5)  # short interval, adjustable
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
            "custom_buttons": [],  # list[str] - button labels (space-separated created via /button)
            "panel_message": None  # optional: store last panel message id
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
    # priority: configured log_channel -> #logs-ticket channel -> create
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

# === UTILITY: create a view with buttons (standard + custom) ===
def make_ticket_view(guild_id: int) -> discord.ui.View:
    """
    Build a View containing standard ticket buttons + custom buttons defined per-guild.
    Each button is given a persistent custom_id including guild_id and index.
    """
    gid = str(guild_id)
    gconf = config.data.get(gid, {})
    custom = gconf.get("custom_buttons", []) if gconf else []

    view = discord.ui.View(timeout=None)  # persistent

    # helper to attach callback for a button label
    async def make_callback(ticket_type: str, custom_id: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            # reuse create ticket logic below (call shared function)
            await create_ticket_from_interaction(interaction, ticket_type)
        return callback

    # standard buttons
    standard = [("🛒 Mua hàng", "Mua hàng"), ("⚡ Cày thuê", "Cày thuê"),
                ("🛠️ Báo lỗi", "Báo lỗi"), ("📩 Khác", "Khác")]

    # add standard buttons (these will occupy indices 0..n-1)
    for idx, (label, ttype) in enumerate(standard):
        cid = f"ticket_std_{gid}_{idx}"
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=cid)
        # assign callback
        async def gen_cb(tt=ttype, _cid=cid):
            async def cb(interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True)
                await create_ticket_from_interaction(interaction, tt)
            return cb
        btn.callback = asyncio.get_event_loop().run_until_complete(gen_cb()) if False else None
        # can't run coroutine to generate callback here; we'll set callback below using closure
        # set callback properly:
        async def cb_factory(ttype):
            async def cb(interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True)
                await create_ticket_from_interaction(interaction, ttype)
            return cb
        # but we can't await here; instead define closure below:
        def make_cb(ttype):
            async def cb(interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True)
                await create_ticket_from_interaction(interaction, ttype)
            return cb
        btn.callback = make_cb(ttype)
        view.add_item(btn)

    # add custom buttons
    # custom labels stored in config as plain names (no emoji). We'll create buttons with those labels.
    for cidx, label in enumerate(custom):
        cid = f"ticket_custom_{gid}_{cidx}"
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=cid)
        # callback
        def make_cb_l(lbl):
            async def cb(interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True)
                await create_ticket_from_interaction(interaction, lbl)
            return cb
        btn.callback = make_cb_l(label)
        view.add_item(btn)

    return view

# Shared ticket creation logic (extracted)
async def create_ticket_from_interaction(interaction: discord.Interaction, ticket_type: str):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data.get(gid, {})
    category_id = gconf.get("ticket_category")
    category = interaction.guild.get_channel(category_id) if category_id else None
    if not category:
        return await interaction.followup.send("❌ Ticket system chưa được setup (category missing).", ephemeral=True)

    # blacklist check
    reason = is_blacklisted(interaction.guild, interaction.user)
    if reason:
        return await interaction.followup.send(f"🚫 Bạn đã bị blacklist theo {reason}!", ephemeral=True)

    # create ticket id & channel
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

    safe_type = ticket_type.replace(" ", "").lower()
    ch_name = f"ticket-{tid}-{safe_type}"
    ch = await category.create_text_channel(name=ch_name, overwrites=overwrites)

    tickets.data["tickets"][str(ch.id)] = {"id": tid, "user": interaction.user.id, "type": ticket_type}
    tickets.mark_dirty()

    await interaction.followup.send(f"✅ Ticket #{tid} (**{ticket_type}**) đã được tạo: {ch.mention}", ephemeral=True)
    await ch.send(f"🎟️ Ticket #{tid} | {ticket_type} – Xin chào {interaction.user.mention}!")
    await log_ticket_event(interaction.guild, f"🟢 Ticket #{tid} | created by {interaction.user.mention}")

# ====== COMMANDS ======

# setup: category, staff_role, log_channel
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
    await interaction.followup.send(
        f"✅ Ticket system setup!\nCategory: {category.mention}\nStaff: {staff_role.mention}\nLog: {log_channel.mention}",
        ephemeral=True
    )
    await log_ticket_event(interaction.guild, f"⚙️ Ticket system setup by {interaction.user.mention}")

# panel (old embed style) — admin only
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
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/561/561127.png")
    embed.set_footer(text="Ticket System")

    view = make_ticket_view(interaction.guild.id)
    msg = await interaction.channel.send(embed=embed, view=view)
    # store panel message id for possible later update/reference
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    config.data[gid]["panel_message"] = {"channel": interaction.channel.id, "message": msg.id}
    config.mark_dirty()

    await interaction.followup.send("✅ Panel đã được gửi!", ephemeral=True)
    if config.data[gid].get("log_channel"):
        lc = interaction.guild.get_channel(config.data[gid]["log_channel"])
        if lc:
            await lc.send(f"🟢 {interaction.user.mention} vừa gửi panel ticket tại {interaction.channel.mention}")

# close: admin/staff/owner allowed
@bot.tree.command(name="close", description="Đóng ticket và gửi transcript vào logs-ticket")
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
    try:
        await interaction.channel.delete()
    except Exception:
        pass

# rename
@bot.tree.command(name="rename", description="Đổi tên kênh ticket (ghi đè hoàn toàn)")
@is_admin_or_staff_or_owner()
async def rename(interaction: discord.Interaction, new_name: str):
    await interaction.response.defer(ephemeral=True)
    cid = str(interaction.channel.id)
    if cid not in tickets.data.get("tickets", {}):
        return await interaction.followup.send("❌ Đây không phải ticket!", ephemeral=True)
    safe = new_name.replace(" ", "-").lower()
    tickets.data["tickets"][cid]["custom_name"] = safe
    tickets.mark_dirty()
    try:
        await interaction.channel.edit(name=safe)
    except Exception:
        pass
    await interaction.followup.send(f"✏️ Đã đổi tên ticket thành **{safe}**!", ephemeral=True)
    await log_ticket_event(interaction.guild, f"✏️ Ticket #{tickets.data['tickets'][cid]['id']} renamed by {interaction.user.mention}")

# add
@bot.tree.command(name="add", description="Thêm người dùng vào ticket hiện tại")
@is_admin_or_staff_or_owner()
async def add(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    cid = str(interaction.channel.id)
    if cid not in tickets.data.get("tickets", {}):
        return await interaction.followup.send("❌ Đây không phải ticket!", ephemeral=True)
    await interaction.channel.set_permissions(member, view_channel=True, send_messages=True)
    await interaction.followup.send(f"✅ Đã thêm {member.mention} vào ticket!", ephemeral=True)
    await log_ticket_event(interaction.guild, f"👤 {member.mention} added to Ticket #{tickets.data['tickets'][cid]['id']} by {interaction.user.mention}")

# blacklist by id (user or role)
@bot.tree.command(name="blacklist", description="Thêm ID vào blacklist (user hoặc role)")
@app_commands.checks.has_permissions(administrator=True)
async def blacklist_cmd(interaction: discord.Interaction, target_id: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    try:
        tid = int(target_id)
    except ValueError:
        return await interaction.followup.send("❌ ID không hợp lệ!", ephemeral=True)
    member = guild.get_member(tid)
    role = guild.get_role(tid)
    if member:
        if tid in blacklist.data["users"]:
            return await interaction.followup.send(f"❌ {member.mention} đã có trong blacklist!", ephemeral=True)
        blacklist.data["users"].append(tid)
        blacklist.mark_dirty()
        await interaction.followup.send(f"🚫 Đã thêm {member.mention} vào blacklist!", ephemeral=True)
        await log_ticket_event(guild, f"🚫 {member.mention} added to blacklist by {interaction.user.mention}")
    elif role:
        if tid in blacklist.data["roles"]:
            return await interaction.followup.send(f"❌ {role.mention} đã có trong blacklist!", ephemeral=True)
        blacklist.data["roles"].append(tid)
        blacklist.mark_dirty()
        await interaction.followup.send(f"🚫 Đã thêm role {role.mention} vào blacklist!", ephemeral=True)
        await log_ticket_event(guild, f"🚫 Role {role.mention} added to blacklist by {interaction.user.mention}")
    else:
        await interaction.followup.send("❌ Không tìm thấy member hoặc role với ID này!", ephemeral=True)

# unblacklist
@bot.tree.command(name="unblacklist", description="Gỡ ID khỏi blacklist (user hoặc role)")
@app_commands.checks.has_permissions(administrator=True)
async def unblacklist_cmd(interaction: discord.Interaction, target_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        tid = int(target_id)
    except ValueError:
        return await interaction.followup.send("❌ ID không hợp lệ!", ephemeral=True)
    if tid in blacklist.data["users"]:
        blacklist.data["users"].remove(tid)
        blacklist.mark_dirty()
        return await interaction.followup.send(f"✅ Đã gỡ user ID `{tid}` khỏi blacklist!", ephemeral=True)
    if tid in blacklist.data["roles"]:
        blacklist.data["roles"].remove(tid)
        blacklist.mark_dirty()
        return await interaction.followup.send(f"✅ Đã gỡ role ID `{tid}` khỏi blacklist!", ephemeral=True)
    await interaction.followup.send("❌ ID này không có trong blacklist!", ephemeral=True)

# ====== NEW: /button (add custom buttons, multiple names separated by spaces) ======
@bot.tree.command(name="button", description="Add one or more custom ticket buttons (space separated names)")
@is_admin_or_staff()
async def button_cmd(interaction: discord.Interaction, names: str):
    """
    Example: /button Support Refund Billing
    Adds three persistent buttons labeled "Support", "Refund", "Billing"
    """
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    # split by spaces; ignore empty
    parts = [p.strip() for p in names.split() if p.strip()]
    if not parts:
        return await interaction.followup.send("❌ Vui lòng cung cấp tên button, phân tách bởi dấu cách.", ephemeral=True)
    # Append new custom buttons (avoid duplicates)
    existing = set(gconf.get("custom_buttons", []))
    added = []
    for p in parts:
        if p not in existing:
            gconf.setdefault("custom_buttons", []).append(p)
            existing.add(p)
            added.append(p)
    if added:
        config.mark_dirty()
        # register view for these buttons immediately so they work without restart
        bot.add_view(make_ticket_view(int(gid)))
        await interaction.followup.send(f"✅ Đã thêm các button: {', '.join(added)}", ephemeral=True)
        await log_ticket_event(interaction.guild, f"⚙️ Added buttons {', '.join(added)} by {interaction.user.mention}")
    else:
        await interaction.followup.send("❌ Không có button mới (các tên đã tồn tại).", ephemeral=True)

# ====== NEW: /re_sync (fast panel renew) ======
@bot.tree.command(name="re_sync", description="Re-send/renew the panel embed with current buttons")
@is_admin_or_staff()
async def resync_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    # Build embed (old style)
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
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/561/561127.png")
    embed.set_footer(text="Ticket System")

    view = make_ticket_view(interaction.guild.id)
    msg = await interaction.channel.send(embed=embed, view=view)
    # update stored panel_message
    gconf["panel_message"] = {"channel": interaction.channel.id, "message": msg.id}
    config.mark_dirty()
    # register view persistently now
    bot.add_view(view)
    await interaction.followup.send("✅ Panel re-synced successfully!", ephemeral=True)
    await log_ticket_event(interaction.guild, f"🔁 Panel re-synced by {interaction.user.mention}")

# ====== READY & STARTUP ======
@bot.event
async def on_ready():
    # start periodic saver
    bot.loop.create_task(periodic_saver())
    # register persistent views for each guild based on config
    for gid, gconf in config.data.items():
        try:
            vid = int(gid)
            bot.add_view(make_ticket_view(vid))
        except Exception:
            pass
    # ensure standard view is also added (for safety)
    # bot.add_view(make_ticket_view(...)) already handled per-guild above
    print(f"✅ Logged in as {bot.user} — {len(config.data)} guild(s) loaded")
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Failed to sync commands:", e)

# ====== RUN ======
if __name__ == "__main__":
    keep_alive()
    load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in environment (.env)")
bot.run(TOKEN)
