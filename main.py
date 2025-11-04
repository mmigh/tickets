import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio, json, os, datetime

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

CONFIG_FILE = "config.json"
TICKET_FILE = "tickets.json"

# ========= CONFIG HANDLER =========
class JSONData:
    def __init__(self, filename):
        self.filename = filename
        self.data = {}
        self.load()

    def load(self):
        if os.path.exists(self.filename):
            with open(self.filename, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = {}
            self.save()

    def save(self):
        with open(self.filename, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=4, ensure_ascii=False)

    def mark_dirty(self):
        self.save()


config = JSONData(CONFIG_FILE)
tickets = JSONData(TICKET_FILE)

# ========= UTILS =========
def ensure_guild_config(gid: str):
    if gid not in config.data:
        config.data[gid] = {
            "ticket_category": None,
            "staff_role": None,
            "log_channel": None,
            "buttons": [],
            "panel_message": None
        }
        config.mark_dirty()

async def ensure_logs_channel(guild: discord.Guild):
    gid = str(guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    channel_id = gconf.get("log_channel")
    if not channel_id:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False)
        }
        ch = await guild.create_text_channel("logs-ticket", overwrites=overwrites)
        gconf["log_channel"] = ch.id
        config.mark_dirty()
        return ch
    ch = guild.get_channel(channel_id)
    if not ch:
        ch = await guild.create_text_channel("logs-ticket")
        gconf["log_channel"] = ch.id
        config.mark_dirty()
    return ch

def is_blacklisted(guild: discord.Guild, user: discord.Member):
    gid = str(guild.id)
    gconf = config.data.get(gid, {})
    bl = gconf.get("blacklist", [])
    for e in bl:
        if e["user"] == user.id:
            return e["reason"]
    return None

# ========= LOGGING =========
async def log_ticket_event(guild: discord.Guild, message: str):
    try:
        ch = await ensure_logs_channel(guild)
        await ch.send(message)
    except Exception as e:
        print("Log failed:", e)

# ========= MAKE VIEW =========
class TicketButton(discord.ui.Button):
    def __init__(self, label, style, emoji, ticket_type):
        super().__init__(label=label, style=style, emoji=emoji)
        self.ticket_type = ticket_type

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_ticket_from_interaction(interaction, self.ticket_type)

def make_ticket_view(gid: int):
    gconf = config.data.get(str(gid), {})
    view = discord.ui.View(timeout=None)
    for b in gconf.get("buttons", []):
        view.add_item(TicketButton(b["label"], discord.ButtonStyle.primary, b.get("emoji"), b["type"]))
    return view

# ========= TICKET CREATION =========
async def create_ticket_from_interaction(interaction: discord.Interaction, ticket_type: str):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data.get(gid, {})

    category_id = gconf.get("ticket_category")
    category = interaction.guild.get_channel(category_id) if category_id else None
    if not category:
        return await interaction.followup.send("❌ Ticket system chưa được setup.", ephemeral=True)

    reason = is_blacklisted(interaction.guild, interaction.user)
    if reason:
        return await interaction.followup.send(f"🚫 Bạn đã bị blacklist ({reason})!", ephemeral=True)

    next_id = tickets.data.get("next_id", 1)
    tid = next_id
    tickets.data["next_id"] = tid + 1

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
    ch = await category.create_text_channel(name=f"ticket-{tid}-{safe_type}", overwrites=overwrites)

    tickets.data.setdefault("tickets", {})
    tickets.data["tickets"][str(ch.id)] = {
        "id": tid,
        "user": interaction.user.id,
        "type": ticket_type
    }
    tickets.mark_dirty()

    await ch.send(f"🎟️ Ticket #{tid} | {ticket_type}\nXin chào {interaction.user.mention}!")
    await interaction.followup.send(f"✅ Ticket **#{tid}** đã được tạo: {ch.mention}", ephemeral=True)

    await log_ticket_event(interaction.guild, f"🟢 Ticket **#{tid}** opened by {interaction.user.mention}")

# ========= COMMANDS =========
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")

def is_admin_or_staff_or_owner():
    async def predicate(interaction: discord.Interaction):
        if interaction.user.guild_permissions.administrator:
            return True
        gid = str(interaction.guild.id)
        role_id = config.data.get(gid, {}).get("staff_role")
        if role_id and discord.utils.get(interaction.user.roles, id=role_id):
            return True
        if interaction.user == interaction.guild.owner:
            return True
        raise app_commands.CheckFailure("Bạn không có quyền thực hiện lệnh này.")
    return app_commands.check(predicate)

# --- /setup ---
@bot.tree.command(name="setup", description="Thiết lập hệ thống ticket")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    gconf["ticket_category"] = category.id
    gconf["staff_role"] = staff_role.id
    config.mark_dirty()
    await interaction.response.send_message("✅ Đã thiết lập hệ thống ticket thành công!", ephemeral=True)

# --- /panel ---
@bot.tree.command(name="panel", description="Gửi panel mở ticket")
@app_commands.checks.has_permissions(administrator=True)
async def panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(
        title="⚡ Open Ticket – Giải quyết nhanh chóng",
        description="Chọn loại hỗ trợ bên dưới:",
        color=discord.Color.blue()
    )
    view = make_ticket_view(interaction.guild.id)
    msg = await interaction.channel.send(embed=embed, view=view)

    gid = str(interaction.guild.id)
    config.data[gid]["panel_message"] = {"channel": interaction.channel.id, "message": msg.id}
    config.mark_dirty()
    await interaction.followup.send("✅ Panel đã được gửi!", ephemeral=True)

# --- /close ---
@bot.tree.command(name="close", description="Đóng ticket")
@is_admin_or_staff_or_owner()
async def close(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cid = str(interaction.channel.id)
    if cid not in tickets.data.get("tickets", {}):
        return await interaction.followup.send("❌ Đây không phải ticket!", ephemeral=True)

    info = tickets.data["tickets"].pop(cid)
    tickets.mark_dirty()
    await log_ticket_event(interaction.guild, f"🔴 Ticket **#{info['id']}** closed by {interaction.user.mention}")

    await interaction.followup.send("✅ Ticket đã được đóng!", ephemeral=True)
    await asyncio.sleep(3)
    try:
        await interaction.channel.delete()
    except:
        pass

# --- /set_id ---
@bot.tree.command(name="set_id", description="Đặt lại ID khởi đầu cho ticket")
@app_commands.checks.has_permissions(administrator=True)
async def set_id(interaction: discord.Interaction, start_id: int):
    await interaction.response.defer(ephemeral=True)
    if start_id < 1:
        return await interaction.followup.send("❌ ID phải >= 1.", ephemeral=True)
    tickets.data["next_id"] = start_id
    tickets.mark_dirty()
    await interaction.followup.send(f"✅ Ticket ID bắt đầu đã đặt thành **{start_id}**", ephemeral=True)

# --- /refresh_panel ---
@bot.tree.command(name="refresh_panel", description="Làm mới embed panel mà không xoá message")
@app_commands.checks.has_permissions(administrator=True)
async def refresh_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild.id)
    gconf = config.data.get(gid, {})
    panel_info = gconf.get("panel_message")

    if not panel_info:
        return await interaction.followup.send("❌ Chưa có panel nào được lưu!", ephemeral=True)

    channel = interaction.guild.get_channel(panel_info["channel"])
    if not channel:
        return await interaction.followup.send("❌ Không tìm thấy kênh panel!", ephemeral=True)

    try:
        msg = await channel.fetch_message(panel_info["message"])
    except discord.NotFound:
        return await interaction.followup.send("❌ Không tìm thấy message panel cũ!", ephemeral=True)

    embed = discord.Embed(
        title="⚡ Open Ticket – Giải quyết nhanh chóng",
        description="Chọn loại hỗ trợ bên dưới:",
        color=discord.Color.blue()
    )
    view = make_ticket_view(interaction.guild.id)
    await msg.edit(embed=embed, view=view)

    await interaction.followup.send("✅ Panel đã được làm mới!", ephemeral=True)

# --- /re_sync ---
@bot.tree.command(name="re_sync", description="Đồng bộ lại commands")
@app_commands.checks.has_permissions(administrator=True)
async def re_sync(interaction: discord.Interaction):
    await bot.tree.sync()
    await interaction.response.send_message("✅ Đã re-sync commands!", ephemeral=True)

# --- /add_button ---
@bot.tree.command(name="add_button", description="Thêm nút mới cho panel")
@app_commands.checks.has_permissions(administrator=True)
async def add_button(interaction: discord.Interaction, label: str, emoji: str, ticket_type: str):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    config.data[gid]["buttons"].append({"label": label, "emoji": emoji, "type": ticket_type})
    config.mark_dirty()
    await interaction.response.send_message("✅ Đã thêm nút!", ephemeral=True)

# --- /remove_button ---
@bot.tree.command(name="remove_button", description="Xoá nút khỏi panel")
@app_commands.checks.has_permissions(administrator=True)
async def remove_button(interaction: discord.Interaction, label: str):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    gconf["buttons"] = [b for b in gconf["buttons"] if b["label"] != label]
    config.mark_dirty()
    await interaction.response.send_message("✅ Đã xoá nút!", ephemeral=True)

# --- /blacklist ---
@bot.tree.command(name="blacklist", description="Thêm người vào blacklist")
@app_commands.checks.has_permissions(administrator=True)
async def blacklist(interaction: discord.Interaction, user: discord.Member, reason: str):
    gid = str(interaction.guild.id)
    ensure_guild_config(gid)
    gconf = config.data[gid]
    gconf.setdefault("blacklist", []).append({"user": user.id, "reason": reason})
    config.mark_dirty()
    await interaction.response.send_message(f"🚫 Đã blacklist {user.mention}", ephemeral=True)

# --- /rename ---
@bot.tree.command(name="rename", description="Đổi tên ticket hiện tại")
@is_admin_or_staff_or_owner()
async def rename(interaction: discord.Interaction, new_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.channel.edit(name=new_name)
        await interaction.followup.send("✅ Đã đổi tên ticket!", ephemeral=True)
    except:
        await interaction.followup.send("❌ Lỗi khi đổi tên!", ephemeral=True)

# ========= RUN =========
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing in environment (.env)")
bot.run(TOKEN)

