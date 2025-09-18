import asyncio
import threading
from datetime import datetime, timedelta
import sqlite3
import logging
import requests
from urllib.parse import quote
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# ==============================
# Config - REPLACE placeholders
# ==============================
TELEGRAM_TOKEN = "8328524282:AAHsjZGe5oACx0mveExVeTG3JJphDDDEUGc"
CLIENT_ID = "943f520c89b84653b1ee33577e618936"
CLIENT_SECRET = "086700b7cdb64247a51b96b9f2cbcacd"
REDIRECT_URI = "https://spotify-callback-vercel-3p3nt2obv-chandus-projects-19a3c7a3.vercel.app/api/callback"
GENIUS_TOKEN = "ycFu6h35kczI-6T8OnhynV1QVN0Ip_S_9khc2bcLFRrZnxdRFCKx6XMFu8zLrmMe"  # for lyrics

# HTTP server settings - FIXED PORT
HTTP_HOST = "0.0.0.0"
HTTP_PORT = 20802  # <-- FIXED: Port now matches your REDIRECT_URI

# ==============================
# Spotify API Endpoints - FIXED URLS
# ==============================
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"

# ==============================
# Logging
# ==============================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==============================
# Database
# ==============================
conn = sqlite3.connect("spotify_users.db", check_same_thread=False)
cursor = conn.cursor()

# Create table if it doesn't exist
cursor.execute(
    """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    refresh_token TEXT NOT NULL,
    access_token TEXT,
    expires_at TEXT
)
"""
)
conn.commit()

# Add playlist_id column if missing
try:
    cursor.execute("ALTER TABLE users ADD COLUMN playlist_id TEXT")
    conn.commit()
except sqlite3.OperationalError:
    # Column already exists
    pass

def store_refresh_token(telegram_id: int, refresh_token: str):
    cursor.execute(
        """
    INSERT INTO users (telegram_id, refresh_token)
    VALUES (?, ?)
    ON CONFLICT(telegram_id) DO UPDATE SET refresh_token=excluded.refresh_token
    """,
        (telegram_id, refresh_token),
    )
    conn.commit()

def update_access_token(telegram_id: int, access_token: str, expires_in: int):
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    cursor.execute(
        """
    UPDATE users SET access_token=?, expires_at=? WHERE telegram_id=?
    """,
        (access_token, expires_at.isoformat(), telegram_id),
    )
    conn.commit()

def set_user_playlist(telegram_id: int, playlist_id: str):
    cursor.execute("SELECT 1 FROM users WHERE telegram_id=?", (telegram_id,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO users (telegram_id, refresh_token) VALUES (?, ?)", (telegram_id, ""))
    cursor.execute(
        """
    UPDATE users SET playlist_id=? WHERE telegram_id=?
    """,
        (playlist_id, telegram_id),
    )
    conn.commit()

def get_user_playlist(telegram_id: int):
    cursor.execute(
        "SELECT playlist_id FROM users WHERE telegram_id=?", (telegram_id,)
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] else None

def get_user_tokens(telegram_id: int):
    cursor.execute(
        "SELECT refresh_token, access_token, expires_at FROM users WHERE telegram_id=?",
        (telegram_id,),
    )
    row = cursor.fetchone()
    if row:
        refresh_token, access_token, expires_at = row
        expires_at_dt = datetime.fromisoformat(expires_at) if expires_at else None
        return refresh_token, access_token, expires_at_dt
    return None, None, None

# ==============================
# Spotify Helpers
# ==============================
def refresh_access_token(refresh_token: str):
    resp = requests.post(
        SPOTIFY_TOKEN_URL,  # <-- FIXED
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    ).json()
    access_token = resp.get("access_token")
    expires_in = resp.get("expires_in", 3600)
    return access_token, expires_in

def get_valid_token(user_id: int):
    refresh_token, access_token, expires_at = get_user_tokens(user_id)
    if not refresh_token:
        return None
    if not access_token or (expires_at and datetime.utcnow() >= expires_at):
        access_token, expires_in = refresh_access_token(refresh_token)
        if not access_token:
            return None
        update_access_token(user_id, access_token, expires_in)
    return access_token

def spotify_get(url, token):
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    return r.json() if r.status_code == 200 else None

def spotify_post(url, token, data=None):
    r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=data)
    return r.status_code in (200, 201, 204)

def spotify_put(url, token, data=None):
    r = requests.put(url, headers={"Authorization": f"Bearer {token}"}, json=data)
    return r.status_code in (200, 201, 204)

def spotify_delete(url, token, data=None):
    r = requests.delete(url, headers={"Authorization": f"Bearer {token}"}, json=data)
    return r.status_code in (200, 201, 204)

def get_current_song(token):
    # Try currently playing first
    data = spotify_get(f"{SPOTIFY_API_BASE_URL}/me/player/currently-playing", token) # <-- FIXED
    if data and data.get("item"):
        song = data["item"]["name"]
        artist = ", ".join([a["name"] for a in data["item"]["artists"]])
        album = data["item"]["album"]["name"]
        image = data["item"]["album"]["images"][0]["url"] if data["item"]["album"].get("images") else None
        url = data["item"]["external_urls"]["spotify"]
        return song, artist, album, image, url
    
    # Fallback to recently played
    data = spotify_get(f"{SPOTIFY_API_BASE_URL}/me/player/recently-played?limit=1", token) # <-- FIXED
    if data and data.get("items"):
        track = data["items"][0]["track"]
        song = track["name"]
        artist = ", ".join([a["name"] for a in track["artists"]])
        album = track["album"]["name"]
        image = track["album"]["images"][0]["url"] if track["album"].get("images") else None
        url = track["external_urls"]["spotify"]
        return song, artist, album, image, url
        
    return None

def search_song(query: str, token: str):
    data = spotify_get(f"{SPOTIFY_API_BASE_URL}/search?q={quote(query)}&type=track&limit=5", token) # <-- FIXED
    results = []
    if data and "tracks" in data:
        for item in data["tracks"]["items"]:
            results.append(
                {
                    "name": item["name"],
                    "artist": ", ".join([a["name"] for a in item["artists"]]),
                    "url": item["external_urls"]["spotify"],
                }
            )
    return results

def toggle_playback(token):
    playback = spotify_get(f"{SPOTIFY_API_BASE_URL}/me/player", token) # <-- FIXED
    if playback and playback.get("is_playing"):
        return spotify_put(f"{SPOTIFY_API_BASE_URL}/me/player/pause", token) # <-- FIXED
    else:
        return spotify_put(f"{SPOTIFY_API_BASE_URL}/me/player/play", token) # <-- FIXED

def next_track(token):
    return spotify_post(f"{SPOTIFY_API_BASE_URL}/me/player/next", token) # <-- FIXED

def add_current_to_playlist(token, playlist_id):
    song_data = get_current_song(token)
    if not song_data:
        return False
    _, _, _, _, url = song_data
    track_id = url.split("/")[-1]
    return spotify_post(f"{SPOTIFY_API_BASE_URL}/playlists/{playlist_id}/tracks?uris=spotify:track:{track_id}", token) # <-- FIXED

def remove_current_from_playlist(token, playlist_id):
    song_data = get_current_song(token)
    if not song_data:
        return False
    _, _, _, _, url = song_data
    track_id = url.split("/")[-1]
    return spotify_delete(
        f"{SPOTIFY_API_BASE_URL}/playlists/{playlist_id}/tracks", # <-- FIXED
        token,
        data={"tracks": [{"uri": f"spotify:track:{track_id}"}]},
    )

def fetch_lyrics(song_name: str, artist_name: str):
    headers = {"Authorization": f"Bearer {GENIUS_TOKEN}"}
    query = f"{song_name} {artist_name}"
    resp = requests.get(f"https://api.genius.com/search?q={quote(query)}", headers=headers).json()
    if resp.get("response") and resp["response"]["hits"]:
        return resp["response"]["hits"][0]["result"]["url"]
    return None

# ==============================
# FastAPI Callback
# ==============================
app = FastAPI()
bot: Bot = None
loop: asyncio.AbstractEventLoop = None

@app.get("/callback", response_class=HTMLResponse)
async def spotify_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return HTMLResponse("<h2>Missing code or state ❌</h2>", status_code=400)
    try:
        user_id = int(state)
    except:
        return HTMLResponse("<h2>Invalid state ❌</h2>", status_code=400)
    
    resp = requests.post(
        SPOTIFY_TOKEN_URL,  # <-- FIXED
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    ).json()

    if "error" in resp:
        return HTMLResponse(f"<h2>Login failed ❌<br>{resp.get('error_description')}</h2>", status_code=400)
    
    refresh_token = resp.get("refresh_token")
    access_token = resp.get("access_token")
    expires_in = resp.get("expires_in", 3600)
    
    if refresh_token:
        store_refresh_token(user_id, refresh_token)
        update_access_token(user_id, access_token, expires_in)
        # notify user in Telegram (ensure bot & loop are set)
        try:
            if bot and loop:
                asyncio.run_coroutine_threadsafe(
                    bot.send_message(user_id, "✅ Spotify login successful! You can now use /nowplaying"),
                    loop,
                )
        except Exception as e:
            logger.exception("Failed to notify user after callback: %s", e)
        return HTMLResponse("<h2>Login successful! ✅ Close this page and return to Telegram.</h2>")
        
    return HTMLResponse("<h2>Login failed ❌</h2>", status_code=400)

# ==============================
# Telegram Bot
# ==============================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start", "help"))
async def cmd_start_help(message: types.Message):
    help_text = (
        "🤖 <b>Spotify Bot Commands</b>\n\n"
        "/login - Connect your Spotify account\n"
        "/nowplaying - Show current playing track with controls\n"
        "/setplaylist &lt;playlist_id&gt; - Set your playlist to add songs to\n"
        "/search &lt;query&gt; - Search for a song\n"
        "/lyrics &lt;song&gt; - Get lyrics (or just /lyrics while playing)\n\n"
        "ℹ️ <b>Tips:</b>\n"
        "- Use /login first.\n"
        "- Find your playlist ID from the Spotify URL (e.g., .../playlist/<b>THIS_IS_THE_ID</b>?si=...)\n"
    )
    await message.answer(help_text, parse_mode="HTML")

@dp.message(Command("login"))
async def login(message: types.Message):
    user_id = message.from_user.id
    auth_url = (
        f"{SPOTIFY_AUTH_URL}?client_id={CLIENT_ID}"  # <-- FIXED
        "&response_type=code"
        f"&redirect_uri={quote(REDIRECT_URI)}"
        "&scope=user-read-currently-playing%20user-read-playback-state%20user-read-recently-played%20playlist-modify-public%20playlist-modify-private%20user-modify-playback-state"
        f"&state={user_id}&show_dialog=true"
    )
    await message.answer(f"Click here to login to Spotify:\n{auth_url}")

@dp.message(Command("setplaylist"))
async def set_playlist(message: types.Message, command: CommandObject):
    if not command.args:
        await message.answer("❌ Usage: /setplaylist <playlist_id>")
        return
    playlist_id = command.args.strip()
    set_user_playlist(message.from_user.id, playlist_id)
    await message.answer(f"✅ Playlist set! ID: `{playlist_id}`", parse_mode="Markdown")

@dp.message(Command("nowplaying"))
async def now_playing(message: types.Message):
    user_id = message.from_user.id
    token = get_valid_token(user_id)
    if not token:
        await message.answer("⚠️ You need to /login first.")
        return
        
    song_data = get_current_song(token)
    if song_data:
        song, artist, album, image, url = song_data
        
        buttons = [
            [
                InlineKeyboardButton(text="⏯️ Play/Pause", callback_data="toggle"),
                InlineKeyboardButton(text="⏭️ Next", callback_data="next")
            ]
        ]
        
        # Only show Add/Remove buttons if a playlist is set
        playlist_id = get_user_playlist(user_id)
        if playlist_id:
            buttons.append([
                InlineKeyboardButton(text="💖 Add to Playlist", callback_data="add"),
                InlineKeyboardButton(text="❌ Remove from Playlist", callback_data="remove")
            ])
        
        reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        caption = f"🎵 *{song}*\n👨‍🎤 _{artist}_\n💿 {album}\n\n[Open on Spotify]({url})"
        
        if image:
            await message.answer_photo(photo=image, caption=caption, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await message.answer(caption, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await message.answer("❌ No song is currently playing or available in recently played.")

@dp.message(Command("search"))
async def search(message: types.Message, command: CommandObject):
    token = get_valid_token(message.from_user.id)
    if not token:
        await message.answer("⚠️ You need to /login first.")
        return
        
    query = command.args.strip() if command.args else ""
    if not query:
        await message.answer("❌ Usage: /search <song or artist>")
        return
        
    results = search_song(query, token)
    if results:
        text = "🔎 *Search Results:*\n\n"
        for r in results:
            text += f"🎵 *{r['name']}* by _{r['artist']}_\n[Link]({r['url']})\n\n"
        await message.answer(text, parse_mode="Markdown")
    else:
        await message.answer("❌ No results found.")

@dp.message(Command("lyrics"))
async def lyrics(message: types.Message, command: CommandObject):
    args = command.args.strip() if command.args else ""
    token = get_valid_token(message.from_user.id)
    
    if args:
        if " - " in args:
            song_name, artist_name = [s.strip() for s in args.split(" - ", 1)]
        else:
            song_name = args
            artist_name = ""
    else:
        if not token:
            await message.answer("⚠️ You need to /login first to get lyrics for the current song.")
            return
        now = get_current_song(token)
        if not now:
            await message.answer("⚠️ No song playing and no query provided. Use `/lyrics <song> - <artist>` or play a song.", parse_mode="Markdown")
            return
        song_name, artist_name, _, _, _ = now

    genius_url = fetch_lyrics(song_name, artist_name)
    if genius_url:
        await message.answer(f"🎶 Lyrics / Song page (may contain lyrics):\n{genius_url}")
    else:
        await message.answer("❌ Couldn't find lyrics on Genius for that song.")

@dp.callback_query()
async def callback_handler(callback: CallbackQuery):
    data = callback.data
    user_id = callback.from_user.id
    token = get_valid_token(user_id)
    
    await callback.answer()  
    
    if not token:
        await callback.message.reply("⚠️ Your session might have expired. Please /login again.")
        return

    playlist_id = get_user_playlist(user_id)
    if not playlist_id and data in ("add", "remove"):
        await callback.message.reply("⚠️ No playlist set. Use /setplaylist <playlist_id> first.")
        return

    try:
        ok = False
        response_text = "⚠️ Unknown action."
        if data == "toggle":
            ok = toggle_playback(token)
            response_text = "⏯️ Toggled playback." if ok else "❌ Failed to toggle. Is a device active?"
        elif data == "next":
            ok = next_track(token)
            response_text = "⏭️ Skipped to next." if ok else "❌ Failed to skip. Is a device active?"
        elif data == "add":
            ok = add_current_to_playlist(token, playlist_id)
            response_text = "💖 Added to playlist." if ok else "❌ Failed to add. Is something playing?"
        elif data == "remove":
            ok = remove_current_from_playlist(token, playlist_id)
            response_text = "❌ Removed from playlist." if ok else "⚠️ Couldn't remove track."
        
        await callback.message.reply(response_text)
    except Exception as e:
        logger.exception("Callback handler error: %s", e)
        await callback.message.reply("⚠️ An error occurred.")

# ==============================
# Runner
# ==============================
def start_uvicorn_in_thread():
    def run():
        uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_level="info")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    logger.info(f"Started FastAPI on http://{HTTP_HOST}:{HTTP_PORT} in background thread.")

async def start_bot():
    global loop, bot
    loop = asyncio.get_event_loop()
    try:
        logger.info("Starting Telegram polling...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.exception("Polling stopped with exception: %s", e)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    start_uvicorn_in_thread()
    try:
        asyncio.run(start_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")