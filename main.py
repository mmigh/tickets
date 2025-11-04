# tickets_optimized.py — Refactored & optimized ticket system
import asyncio
import json
import os
import signal
from io import BytesIO
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

# ====== FILE PATHS ======
CONFIG_FILE = "config.json"
TICKET_FILE = "tickets.json"
BLACKLIST_FILE = "blacklist.json"

# ====== STORAGE (debounced, safe save) ======
class Storage:
    """
    Simple JSON-backed storage with:
    - in-memory data
    - mark_dirty() to request save
    - debounced auto-save (short delay) + explicit save_now()
    - safe write via temp file -> atomic replace
    """
    def __init__(self, path: str, default: Any, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.path = path
        self._default = default
        self.data = self._load(default)
        self._dirty = False
        self._lock = asyncio.Lock()
        self._save_task: Optional[asyncio.Task] = None
        self._loop = loop or asyncio.get_event_loop()

    def _load(self, default):
        try:
            if not os.path.exists(self.path):
                return default
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # fallback to default (corrupt file)
            return default

    def mark_dirty(self, delay: float = 2.0):
        """
        Mark storage as dirty and schedule a debounced save after `delay` seconds.
        Short delay avoids frequent disk writes while still being responsive.
        """
        self._dirty = True
        if self._save_task and not self._save_task.done():
            # existing scheduled save will handle it
            return
        # schedule save
        self._save_task = self._loop.create_task(self._debounced_save(delay))

    async def _debounced_save(self, delay: float):
        try:
            await asyncio.sleep(delay)
            await self.save_now()
        except asyncio.CancelledError:
            pass

    async def save_now(self):
        """
        Immediately write to disk (async-safe).
        """
        async with self._lock:
            # ensure directory exists
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp_path = self.path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=4, ensure_ascii=False)
                # atomic replace
                os.replace(tmp_path, self.path)
                self._dirty = False
            except Exception as e:
                # try to remove tmp file if present
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                raise

    # synchronous convenience (for shutdown sync points)
    def save_now_sync(self):
        """
        Synchronous save used for shutdown hooks (best-effort).
        """
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, self.path)
            self._dirty = False
        except Exception as e:
            print("Sync save failed:", e)

# Instantiate storages (in-memory)
loop = asyncio.get_event_loop()
config = Storage(CONFIG_FILE, {}, loop)
tickets = Storage(TICKET_FILE, {"last_id": 0, "tickets": {}}, loop)
blacklist = Storage(BLACKLIST_FILE, {"users": [], "roles": []}, loop)

# ====== HELPERS ======
def ensure_guild_config(gid: str):
    if gid not in config.data:
        config.data[gid] = {
            "ticket_category": None,
            "staff_role": None,
            "log_channel": None,
            "custom_buttons": [],
            "panel_message": None,
        }
        config.mark_dirty()

def is_blacklisted(guild: discord.Guild, user: discord.Member) -> Optional[str]:
    if user.id in blacklist.data.get("users", []):
        return "user"
    if any(r.id in blacklist.data.get("roles", []) for r in user.roles):
        return "role"
    return None

async def ensure_logs_channel(guild: discord.Guild) -> discord.TextChannel:
    gid = str(guild.id)
    gconf = config.data.get(gid, {})
    # priority: configured log_channel -> existing "logs-ticket" -> create new
    if gconf.get("log_channel"):
        ch = guild.get_channel(gconf["log_channel"])
        if isinstance(ch, discord.TextChannel):
            return ch
    existing = discord.utils.get(guild.text_channels, name="logs-ticket")
    if existing:
        return existing
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True),
    }
    ch = await guild.create_text_channel("logs-ticket", overwrites=overwrites, reason="Ticket logs channel")
    # update config to remember
    ensure_guild_config(gid)
    config.data[gid]["log_channel"] = ch.id
    config.mark_dirty()
    return ch

async def log_ticket_event(guild: discord.Guild, message: str):
    try:
        ch = await ensure_logs_channel(guild)
        await ch.send(message)
    except Exception as e:
        # avoid raising here so ticket flows still work
        print("Failed to log event:", e)

async def generate_transcript(channel: discord.TextChannel) -> discord.File:
    """
    Produce an HTML transcript as BytesIO (binary) and return discord.File.
    """
    html_parts: List[str] = []
    html_parts.append("<html><head><meta charset='utf-8'><title>{}</title></head><body>".format(channel.name))
    html_parts.append(f"<h2>Transcript of {channel.name}</h2><hr>")
    # fetch messages asynchronously in chronological order
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{m.author} ({m.author.id})"
        content = (m.clean_content or "").replace("\n", "<br>")
        html_parts.append(f"<p><b>[{ts}] {author}:</b> {content}</p>")
        for a in m.attachments:
            html_parts.append(f"<p>📎 <a href='{a.url}'>{a.filename}</a></p>")
    html_parts.append("</body></html>")
    html = "\n".join(html_parts)
    bio = BytesIO(html.encode("utf-8"))
    bio.seek(0)
    return discord.File(fp=bio, filename=f"{channel.name}-transcript.html")

# ====== PERMISSIONS HELPERS (app_commands checks) ======
def is_admin_or_staff():
    async def pred(interaction: discord.Interaction) -> bool:
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
    async def pred(interaction: discord.Interaction) -> bool:
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

# ====== VIEW: Persistent Ticket Panel (class factory) ======
def build_ticket_view(guild_id: int) -> discord.ui.View:
    """
    Return a persistent View instance for the given guild.
    Buttons will have stable custom_id strings so they remain persistent between restarts.
    The callbacks are attached safely using closures with default args to avoid late-binding bugs.
    """
    gid = str(guild_id)
    gconf = config.data.get(gid, {})
    custom = gconf.get("custom_buttons", []) if gconf else []

    view = discord.ui.View(timeout=None)

    # standard buttons list (label, type, style)
    standard = [
        ("🛒 Mua hàng", "Mua hàng", discord.ButtonStyle.secondary),
        ("⚡ Cày thuê", "Cày thuê", discord.ButtonStyle.secondary),
        ("🛠️ Báo lỗi", "Báo lỗi", discord.ButtonStyle.secondary),
        ("📩 Khác", "Khác", discord.ButtonStyle.secondary),
    ]

    # helper to create button with persistent custom_id
    def add_button(label: str, ticket_type: str, idx: int, is_custom: bool = False):
        kind = "custom" if is_custom else "std"
        custom_id = f"ticket_{kind}_{gid}_{idx}"
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary if is_custom else discord.ButtonStyle.secondary, custom_id=custom_id)

        async def button_callback(interaction: discord.Interaction, ttype=ticket_type):
            await interaction.response.defer(ephemeral=True)
            await create_ticket_from_interaction(interaction, ttype)

        # attach callback
        btn.callback = button_callback
        view.add_item(btn)

    # add standard buttons
    for idx, (label, ttype, _) in enumerate(standard):
        add_button(label=label, ticket_type=ttype, idx=idx, is_custom=False)

    # add custom buttons (if any)
    for cidx, label in enumerate(custom):
        add_button(label=label, ticket_type=label, idx=cidx, is_custom=True)

    return view

# ====== TICKET CREATION ======
async def create_ticket_from_interaction(interaction: discord.Interaction, ticket_type: str):
    """
    Centralized ticket creation logic.
    - Check config + blacklist
    - Create channel under configured category with proper overwrites
    - Save ticket meta to tickets storage (debounced)
    - Notify user, log
    """
    if not interaction.guild:
        return await interaction.followup.send("❌ Command only usable in guilds.", ephemeral=True)

    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data.get(gid, {})

    # get category
    category_id = gconf.get("ticket_category")
    category = interaction.guild.get_channel(category_id) if category_id else None
    if not isinstance(category, discord.CategoryChannel):
        return await interaction.followup.send("❌ Ticket system chưa được setup (category missing).", ephemeral=True)

    # blacklist check
    reason = is_blacklisted(interaction.guild, interaction.user)
    if reason:
        return await interaction.followup.send(f"🚫 Bạn đã bị blacklist theo {reason}!", ephemeral=True)

    # increment id safely (in-memory)
    tickets.data["last_id"] = int(tickets.data.get("last_id", 0)) + 1
    tid = tickets.data["last_id"]

    overwrites: Dict[Any, discord.PermissionOverwrite] = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        bot.user: discord.PermissionOverwrite(view_channel=True),
    }
    staff_role_id = gconf.get("staff_role")
    if staff_role_id:
        staff_role = interaction.guild.get_role(staff_role_id)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    safe_type = "".join(ch for ch in ticket_type if ch.isalnum() or ch in "-_").lower() or "ticket"
    ch_name = f"ticket-{tid}-{safe_type}"
    try:
        ch = await category.create_text_channel(name=ch_name, overwrites=overwrites, reason=f"Ticket #{tid} created")
    except Exception as e:
        await interaction.followup.send("❌ Không thể tạo kênh ticket (permission?).", ephemeral=True)
        print("Create channel failed:", e)
        return

    # store ticket meta keyed by channel id string
    tickets.data["tickets"][str(ch.id)] = {"id": tid, "user": interaction.user.id, "type": ticket_type}
    tickets.mark_dirty()

    await interaction.followup.send(f"✅ Ticket #{tid} (**{ticket_type}**) đã được tạo: {ch.mention}", ephemeral=True)
    try:
        await ch.send(f"🎟️ Ticket #{tid} | {ticket_type} – Xin chào {interaction.user.mention}!\nBạn có thể mô tả vấn đề tại đây.")
    except Exception:
        pass
    await log_ticket_event(interaction.guild, f"🟢 Ticket #{tid} | created by {interaction.user.mention}")

# ====== COMMANDS ======

# /setup_ticket
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
        "panel_message": config.data.get(gid, {}).get("panel_message"),
    }
    config.mark_dirty()
    # register persistent view for this guild immediately
    bot.add_view(build_ticket_view(int(gid)))
    await interaction.followup.send(
        f"✅ Ticket system setup!\nCategory: {category.mention}\nStaff: {staff_role.mention}\nLog: {log_channel.mention}",
        ephemeral=True,
    )
    await log_ticket_event(interaction.guild, f"⚙️ Ticket system setup by {interaction.user.mention}")

# /panel (send panel embed with buttons)
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
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/561/561127.png")
    embed.set_footer(text="Ticket System")

    view = build_ticket_view(interaction.guild.id)
    try:
        msg = await interaction.channel.send(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send("❌ Không thể gửi panel (permission?).", ephemeral=True)
        print("Send panel failed:", e)
        return

    # store panel message id for later
    config.data[gid]["panel_message"] = {"channel": interaction.channel.id, "message": msg.id}
    config.mark_dirty()
    bot.add_view(view)  # ensure persistent
    await interaction.followup.send("✅ Panel đã được gửi!", ephemeral=True)
    if config.data[gid].get("log_channel"):
        lc = interaction.guild.get_channel(config.data[gid]["log_channel"])
        if lc:
            await lc.send(f"🟢 {interaction.user.mention} vừa gửi panel ticket tại {interaction.channel.mention}")

# /close
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
    await asyncio.sleep(1)
    try:
        await interaction.channel.delete()
    except Exception:
        pass

# /rename
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

# /add
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

# /blacklist
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

# /unblacklist
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

# /button (add custom buttons)
@bot.tree.command(name="button", description="Add one or more custom ticket buttons (space separated names)")
@is_admin_or_staff()
async def button_cmd(interaction: discord.Interaction, names: str):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    parts = [p.strip() for p in names.split() if p.strip()]
    if not parts:
        return await interaction.followup.send("❌ Vui lòng cung cấp tên button, phân tách bởi dấu cách.", ephemeral=True)
    existing = set(gconf.get("custom_buttons", []))
    added = []
    for p in parts:
        if p not in existing:
            gconf.setdefault("custom_buttons", []).append(p)
            existing.add(p)
            added.append(p)
    if added:
        config.mark_dirty()
        # register new persistent view for this guild
        bot.add_view(build_ticket_view(int(gid)))
        await interaction.followup.send(f"✅ Đã thêm các button: {', '.join(added)}", ephemeral=True)
        await log_ticket_event(interaction.guild, f"⚙️ Added buttons {', '.join(added)} by {interaction.user.mention}")
    else:
        await interaction.followup.send("❌ Không có button mới (các tên đã tồn tại).", ephemeral=True)

# /re_sync (re-send panel)
@bot.tree.command(name="re_sync", description="Re-send/renew the panel embed with current buttons")
@is_admin_or_staff()
async def resync_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    if not config.data[gid].get("ticket_category") or not config.data[gid].get("staff_role"):
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
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/561/561127.png")
    embed.set_footer(text="Ticket System")

    view = build_ticket_view(interaction.guild.id)
    try:
        msg = await interaction.channel.send(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send("❌ Không thể gửi panel (permission?).", ephemeral=True)
        print("Resync send failed:", e)
        return

    config.data[gid]["panel_message"] = {"channel": interaction.channel.id, "message": msg.id}
    config.mark_dirty()
    bot.add_view(view)
    await interaction.followup.send("✅ Panel re-synced successfully!", ephemeral=True)
    await log_ticket_event(interaction.guild, f"🔁 Panel re-synced by {interaction.user.mention}")

# ====== READY & STARTUP ======
@bot.event
async def on_ready():
    # register persistent views for each guild (from config)
    for gid, _ in config.data.items():
        try:
            bot.add_view(build_ticket_view(int(gid)))
        except Exception:
            pass
    print(f"✅ Logged in as {bot.user} — {len(config.data)} guild(s) loaded")
    # attempt to sync commands (catch errors)
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Failed to sync commands:", e)

# ====== SAFE SHUTDOWN HANDLING ======
async def _save_all_storages():
    # attempt async save, but fall back to sync if needed
    try:
        await asyncio.gather(
            config.save_now(),
            tickets.save_now(),
            blacklist.save_now(),
        )
    except Exception as e:
        print("Async save_all failed:", e)
        # fallback to sync saves (best-effort)
        try:
            config.save_now_sync()
            tickets.save_now_sync()
            blacklist.save_now_sync()
        except Exception as e2:
            print("Fallback sync saves failed:", e2)

def _register_shutdown_handlers():
    loop = asyncio.get_event_loop()

    def _sync_save_and_stop():
        # synchronous handler called in the main thread on SIGTERM/SIGINT
        try:
            config.save_now_sync()
            tickets.save_now_sync()
            blacklist.save_now_sync()
        except Exception as e:
            print("Shutdown sync save failed:", e)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sync_save_and_stop)
        except NotImplementedError:
            # Windows or environments that don't support signals in the loop
            pass

_register_shutdown_handlers()

# ====== RUN ======
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing in environment (.env)")
    try:
        bot.run(TOKEN)
    finally:
        # attempt final save on exit (best-effort)
        try:
            config.save_now_sync()
            tickets.save_now_sync()
            blacklist.save_now_sync()
        except Exception:
            pass
