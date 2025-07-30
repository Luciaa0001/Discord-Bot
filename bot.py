import os
import logging
import discord
import requests
import json
from discord.ext import commands
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") # This is your n8n webhook URL
FIREBASE_SERVICE_ACCOUNT_KEY = os.getenv("FIREBASE_SERVICE_ACCOUNT_KEY")

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# --- Firebase Firestore Setup ---
if FIREBASE_SERVICE_ACCOUNT_KEY:
    try:
        # Decode the JSON string from environment variable
        cred_json = json.loads(FIREBASE_SERVICE_ACCOUNT_KEY)
        cred = credentials.Certificate(cred_json)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Firebase Firestore initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")
        db = None
else:
    logger.warning("FIREBASE_SERVICE_ACCOUNT_KEY not set. Webhook configurations will not be persistent.")
    db = None

# --- Bot Setup ---
# Define intents needed for the bot
intents = discord.Intents.default()
intents.message_content = True # Required to read message content
intents.guilds = True        # Required for guild-related events
intents.members = True       # Required for member-related events (e.g., checking admin roles)

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Firestore Helper Functions ---
def get_channel_webhook_ref(guild_id, channel_id):
    """Returns a Firestore document reference for a specific channel's webhook config."""
    if db:
        return db.collection("discord_webhooks").document(f"{guild_id}-{channel_id}")
    return None

async def set_channel_webhook(guild_id, channel_id, webhook_url):
    """Saves the webhook URL for a specific channel to Firestore."""
    ref = get_channel_webhook_ref(guild_id, channel_id)
    if ref:
        await ref.set({"webhook_url": webhook_url, "guild_id": str(guild_id), "channel_id": str(channel_id)})
        return True
    return False

async def get_channel_webhook(guild_id, channel_id):
    """Retrieves the webhook URL for a specific channel from Firestore."""
    ref = get_channel_webhook_ref(guild_id, channel_id)
    if ref:
        doc = await ref.get()
        if doc.exists:
            return doc.to_dict().get("webhook_url")
    return None

async def delete_channel_webhook(guild_id, channel_id):
    """Deletes the webhook URL for a specific channel from Firestore."""
    ref = get_channel_webhook_ref(guild_id, channel_id)
    if ref:
        await ref.delete()
        return True
    return False

async def get_all_guild_webhooks(guild_id):
    """Retrieves all webhook configurations for a given guild."""
    if db:
        webhooks = []
        # Query documents where guild_id matches
        query = db.collection("discord_webhooks").where("guild_id", "==", str(guild_id))
        docs = await query.get()
        for doc in docs:
            webhooks.append(doc.to_dict())
        return webhooks
    return []

# --- Bot Events ---
@bot.event
async def on_ready():
    """Event handler when the bot is ready."""
    logger.info(f"Bot is online as {bot.user} (ID: {bot.user.id})")
    invite_link = (
        f"https://discord.com/oauth2/authorize?"
        f"client_id={bot.user.id}&permissions=277025508352&scope=bot%20applications.commands"
    )
    logger.info(f"Invite the bot using this link:\n{invite_link}")

    try:
        await bot.tree.sync() # Syncs commands globally, might take up to an hour
        # For testing, you can sync to a specific guild for faster updates:
        # guild_id_for_testing = YOUR_GUILD_ID # Replace with your guild ID
        # if guild_id_for_testing:
        #    guild = discord.Object(id=guild_id_for_testing)
        #    await bot.tree.sync(guild=guild)
        logger.info("Slash commands synced successfully.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")

@bot.event
async def on_message(message):
    """Event handler for new messages."""
    if message.author.bot:
        return


    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user.mentioned_in(message)

    channel_webhook_url = None
    if message.guild:
        channel_webhook_url = await get_channel_webhook(message.guild.id, message.channel.id)

    if not (is_dm or is_mention or channel_webhook_url):
        await bot.process_commands(message) 
        return

    logger.info(f"Processing message from {message.author} in {message.channel.name if message.guild else 'DM'}: {message.content}")


    mention_text = f"<@{bot.user.id}>"
    clean_content = message.content.replace(mention_text, "").strip()

    target_webhook_url = channel_webhook_url if channel_webhook_url else WEBHOOK_URL

    if not target_webhook_url:
        logger.warning(f"No WEBHOOK_URL defined for channel {message.channel.name} or globally. Skipping webhook send.")
        await message.channel.send("Error: No webhook URL configured for this channel or globally. Please use `/setup`.")
        await bot.process_commands(message)
        return

    payload = {
        "user": {
            "id": str(message.author.id),
            "username": message.author.name,
            "discriminator": message.author.discriminator,
            "tag": str(message.author),
        },
        "content": clean_content,
        "original_content": message.content,
        "channel": {
            "id": str(message.channel.id),
            "name": getattr(message.channel, "name", "DM"),
            "type": type(message.channel).__name__,
        },
        "guild": {
            "id": str(message.guild.id) if message.guild else None,
            "name": message.guild.name if message.guild else None,
        },
        "message_id": str(message.id),
        "message_link": message.jump_url if message.guild else None,
        "timestamp": message.created_at.isoformat(),
        "source": "mention" if is_mention else ("dm" if is_dm else "channel_webhook_trigger"),
        "is_admin": (
            any(r.permissions.administrator for r in getattr(message.author, "roles", []))
            if message.guild else False
        ),
    }

    logger.info(f"Sending payload to webhook: {json.dumps(payload, indent=2)}")

    try:
        response = requests.post(target_webhook_url, json=payload)
        logger.info(f"Webhook response status: {response.status_code}")
        # Optional: Send a confirmation message to Discord
        # if response.status_code == 200:
        #     await message.channel.send("Message sent to n8n webhook!")
        # else:
        #     await message.channel.send(f"Failed to send message to webhook. Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send webhook: {e}")
        await message.channel.send(f"Error sending message to webhook: {e}")

    await bot.process_commands(message) 



@bot.tree.command(name="setup", description="Set up n8n webhook for this channel")
@commands.has_permissions(manage_channels=True) 
async def setup(interaction: discord.Interaction):
    """Sets up the current channel to send messages to the n8n webhook."""
    if not db:
        await interaction.response.send_message("Database not initialized. Cannot set up webhook.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server channel.", ephemeral=True)
        return


    if not WEBHOOK_URL:
        await interaction.response.send_message("Error: Global WEBHOOK_URL is not configured. Please set it in environment variables.", ephemeral=True)
        return

    success = await set_channel_webhook(interaction.guild.id, interaction.channel.id, WEBHOOK_URL)
    if success:
        await interaction.response.send_message(
            f"Successfully set up n8n webhook for this channel (`{interaction.channel.name}`). "
            "Messages sent here will now be forwarded to n8n.",
            ephemeral=False 
        )
        logger.info(f"Webhook setup for channel {interaction.channel.name} ({interaction.channel.id}) in guild {interaction.guild.name} ({interaction.guild.id})")
    else:
        await interaction.response.send_message("Failed to set up webhook. Please check bot permissions or database connection.", ephemeral=True)

@bot.tree.command(name="remove", description="Remove n8n webhook from this channel")
@commands.has_permissions(manage_channels=True)
async def remove(interaction: discord.Interaction):
    """Removes the webhook configuration for the current channel."""
    if not db:
        await interaction.response.send_message("Database not initialized. Cannot remove webhook.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server channel.", ephemeral=True)
        return

    existing_webhook = await get_channel_webhook(interaction.guild.id, interaction.channel.id)
    if not existing_webhook:
        await interaction.response.send_message("No n8n webhook is set up for this channel.", ephemeral=True)
        return

    success = await delete_channel_webhook(interaction.guild.id, interaction.channel.id)
    if success:
        await interaction.response.send_message(
            f"Successfully removed n8n webhook from this channel (`{interaction.channel.name}`). "
            "Messages will no longer be forwarded to n8n from here.",
            ephemeral=False
        )
        logger.info(f"Webhook removed from channel {interaction.channel.name} ({interaction.channel.id}) in guild {interaction.guild.name} ({interaction.guild.id})")
    else:
        await interaction.response.send_message("Failed to remove webhook. Please check bot permissions or database connection.", ephemeral=True)

@bot.tree.command(name="list", description="List all webhooks in this server")
@commands.has_permissions(manage_channels=True)
async def list_webhooks(interaction: discord.Interaction):
    """Lists all channels with n8n webhook setups in the current server."""
    if not db:
        await interaction.response.send_message("Database not initialized. Cannot list webhooks.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    webhooks = await get_all_guild_webhooks(interaction.guild.id)
    if not webhooks:
        await interaction.response.send_message("No n8n webhooks are set up in this server.", ephemeral=True)
        return

    response_message = "N8N Webhooks configured in this server:\n"
    for webhook_data in webhooks:
        channel_id = int(webhook_data.get("channel_id"))
        channel = interaction.guild.get_channel(channel_id)
        channel_name = channel.name if channel else f"Unknown Channel ({channel_id})"
        response_message += f"- **#{channel_name}**: `{webhook_data.get('webhook_url', 'N/A')}`\n"

    await interaction.response.send_message(response_message, ephemeral=True) 

@bot.tree.command(name="status", description="Show webhook status for this channel")
async def status(interaction: discord.Interaction):
    """Shows the n8n webhook status for the current channel."""
    if not db:
        await interaction.response.send_message("Database not initialized. Cannot check status.", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server channel.", ephemeral=True)
        return

    webhook_url = await get_channel_webhook(interaction.guild.id, interaction.channel.id)
    if webhook_url:
        await interaction.response.send_message(
            f"N8N webhook is **ACTIVE** for this channel (`#{interaction.channel.name}`). "
            f"Messages are forwarded to: `{webhook_url}`",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"N8N webhook is **INACTIVE** for this channel (`#{interaction.channel.name}`). "
            "Use `/setup` to configure it.",
            ephemeral=True
        )

@bot.tree.command(name="privacy", description="View the bot privacy policy")
async def privacy(interaction: discord.Interaction):
    """Displays the bot's privacy policy."""
    privacy_policy_text = (
        "**Privacy Policy for N8N Discord Trigger Bot**\n\n"
        "This bot is designed to forward messages to your configured n8n webhook for automation purposes. "
        "It only processes messages that are direct messages (DMs) to the bot, "
        "mentions of the bot, or messages in channels where a webhook has been explicitly `/setup`.\n\n"
        "**Data Collected:**\n"
        "- **Message Content:** Messages that trigger the bot (DMs, mentions, or in setup channels) are forwarded to your n8n instance.\n"
        "- **User and Channel Information:** User ID, username, channel ID, channel name, guild ID, and guild name are included in the forwarded payload.\n"
        "- **Webhook Configurations:** The bot stores the n8n webhook URL for each channel that uses the `/setup` command. This data is stored in a secure Firestore database.\n\n"
        "**Data Usage:**\n"
        "- The collected data is solely used to facilitate the automation process via your n8n instance.\n"
        "- We do not store your message content or personal data beyond what is necessary for forwarding to n8n and managing webhook configurations.\n\n"
        "**Data Storage:**\n"
        "- Webhook configurations are stored in Google Cloud Firestore.\n"
        "- Your n8n instance is responsible for how it processes and stores the data it receives.\n\n"
        "**Your Control:**\n"
        "- You can `/remove` the webhook configuration from any channel at any time.\n"
        "- You are responsible for the data handling practices of your n8n instance.\n\n"
        "For any questions regarding data privacy, please contact the bot administrator."
    )
    await interaction.response.send_message(privacy_policy_text, ephemeral=True)

@bot.tree.command(name="stats", description="Show bot statistics")
async def stats(interaction: discord.Interaction):
    """Shows basic statistics about the bot."""
    guild_count = len(bot.guilds)
    user_count = sum(guild.member_count for guild in bot.guilds) 
    
    await interaction.response.send_message(
        f"**Bot Statistics:**\n"
        f"- Servers: {guild_count}\n"
        f"- Total Members (across all joined servers): {user_count}\n"
        f"- Latency: {round(bot.latency * 1000)}ms",
        ephemeral=True
    )



@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await interaction.response.send_message(
            f"You don't have the required permissions to use this command. "
            f"You need: {' '.join(error.missing_permissions)}",
            ephemeral=True
        )
    else:
        logger.error(f"An error occurred during command execution: {error}", exc_info=True)
        await interaction.response.send_message(
            "An unexpected error occurred while executing this command. Please try again later.",
            ephemeral=True
        )


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is not set. Exiting.")
        exit(1)
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL is not set. The bot will not be able to send data to n8n without it, but slash commands will still work.")
    if not FIREBASE_SERVICE_ACCOUNT_KEY:
        logger.error("FIREBASE_SERVICE_ACCOUNT_KEY is not set. Webhook configurations will not be persistent. Exiting.")
        exit(1) 

    bot.run(DISCORD_TOKEN)