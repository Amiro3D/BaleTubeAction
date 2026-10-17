#!/usr/bin/env python3
import os
import sys
import json
import time
import secrets
import threading
import urllib.request
import urllib.parse
import subprocess
import re

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ──────────────── ID Generator ────────────────
_id_lock = threading.Lock()
_id_time = 0
_id_counter = 0

def gen_opencode_id(descending: bool) -> str:
    global _id_time, _id_counter
    with _id_lock:
        ts = int(time.time() * 1000)
        if ts != _id_time:
            _id_time = ts
            _id_counter = 0
        _id_counter += 1
        val = ts * 0x1000 + _id_counter
        if descending:
            val = ~val
        hex_parts = [f"{(val >> (40 - 8 * w)) & 0xFF:02x}" for w in range(6)]
        b62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        rand_part = "".join(b62[b % 62] for b in secrets.token_bytes(14))
        return "".join(hex_parts) + rand_part

def gen_opencode_session_id() -> str:
    return "ses_" + gen_opencode_id(descending=True)

def gen_opencode_request_id() -> str:
    return "msg_" + gen_opencode_id(descending=False)

# ──────────────── Natural Pacing ────────────────
_pacing_lock = threading.Lock()
_last_call = 0.0

def pace_request():
    global _last_call
    with _pacing_lock:
        elapsed = time.time() - _last_call
        if elapsed < 2.0:
            time.sleep(2.0 - elapsed)
        _last_call = time.time()

# ──────────────── Bale API Helpers ────────────────
def send_bale_request(method: str, params: dict, token: str):
    if not token:
        print("[Bale Error] No BALE_BOT_TOKEN provided", file=sys.stderr, flush=True)
        return None
    url = f"https://tapi.bale.ai/bot{token}/{method}"
    data = json.dumps(params, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='ignore')
            try:
                j = json.loads(body)
                if j.get("error_code") == 429:
                    retry_after = j.get("parameters", {}).get("retry_after", 3)
                    time.sleep(retry_after + 2)
                    continue
                if e.code in (400, 401, 403, 404):
                    return j
            except Exception:
                pass
            if e.code in (400, 401, 403, 404):
                return None
            time.sleep(attempt + 1)
        except Exception:
            time.sleep(attempt + 1)
    return None

def edit_bale_message(chat_id, message_id, text, token):
    if not message_id or str(message_id) == "0":
        return send_bale_message(chat_id, text, token)
    res = send_bale_request("editMessageText", {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "text": text,
        "parse_mode": "Markdown"
    }, token)
    if not res or not res.get("ok"):
        res = send_bale_request("editMessageText", {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text
        }, token)
    return res

def send_bale_message(chat_id, text, token):
    res = send_bale_request("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }, token)
    if not res or not res.get("ok"):
        res = send_bale_request("sendMessage", {
            "chat_id": chat_id,
            "text": text
        }, token)
    return res

def send_bale_file(method: str, chat_id: str, file_path: str, caption: str, field_name: str, token: str):
    if not token or not file_path or not os.path.exists(file_path):
        return None
    url = f"https://tapi.bale.ai/bot{token}/{method}"
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption[:1024]
    if method == "sendVideo":
        data["supports_streaming"] = "true"

    try:
        import requests
        for attempt in range(4):
            try:
                with open(file_path, "rb") as f:
                    files = {field_name: (os.path.basename(file_path), f)}
                    resp = requests.post(url, data=data, files=files, timeout=120)
                    if resp.status_code == 200:
                        return resp.json()
                    elif resp.status_code == 429:
                        time.sleep(5 * (attempt + 1))
                        continue
                    else:
                        print(f"[Bale File Error] {resp.status_code}: {resp.text}", file=sys.stderr)
            except Exception as e:
                print(f"[Bale File Network Exception] {e}", file=sys.stderr)
                time.sleep(2)
    except ImportError:
        pass
    return None

# ──────────────── Agent Tools Implementation ────────────────
def run_web_search(query: str) -> str:
    print(f"[Agent Tool] Web search: {query}", flush=True)
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
            
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=5))
            formatted = []
            for r in raw_results:
                title = r.get('title', '').strip()
                body = r.get('body', '').strip()
                href = r.get('href', '').strip()
                if title or body:
                    formatted.append(f"• {title}: {body} ({href})")
            return "\n".join(formatted) if formatted else "نتیجه‌ای در جستجو یافت نشد."
    except Exception as e:
        print(f"[Search Error] {e}", file=sys.stderr, flush=True)
        return f"خطا در جستجو: {e}"

def run_read_webpage(url: str) -> str:
    print(f"[Agent Tool] Reading webpage: {url}", flush=True)
    try:
        import requests
        from bs4 import BeautifulSoup
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}, timeout=15)
        resp.encoding = resp.apparent_encoding or 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        for s in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside"]):
            s.decompose()
        title = soup.title.string.strip() if soup.title and soup.title.string else "بدون عنوان"
        text = " ".join(soup.stripped_strings)
        if len(text) > 3500:
            text = text[:3500] + "..."
        return f"عنوان صفحه: {title}\n\nمحتوای متنی صفحه:\n{text}"
    except Exception as e:
        return f"خطا در خواندن صفحه وب: {e}"

def run_read_file(filepath: str) -> str:
    print(f"[Agent Tool] Reading local file: {filepath}", flush=True)
    try:
        clean_path = os.path.basename(filepath)
        if os.path.exists(clean_path):
            with open(clean_path, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()[:2500]
        return f"File '{filepath}' not found or cannot be accessed."
    except Exception as e:
        return f"Error reading file: {e}"

def run_download_media(url: str, media_type: str, chat_id: str, bale_token: str, msg_id: str) -> str:
    print(f"[Agent Tool] Downloading media: {url} (type={media_type})", flush=True)
    edit_bale_message(chat_id, msg_id, f"📥 **در حال دانلود فایل از لینک ارسالی...**\n`{url[:60]}`", bale_token)
    
    dl_dir = os.path.join("/tmp", "agent_dl", str(int(time.time())))
    os.makedirs(dl_dir, exist_ok=True)
    out_tmpl = os.path.join(dl_dir, "%(title).50s.%(ext)s")
    
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--no-warnings",
        "--max-filesize", "48M",
        "-o", out_tmpl
    ]
    if media_type == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    else:
        cmd += ["-f", "bestvideo[filesize<45M]+bestaudio/best[filesize<48M]/best"]
    cmd.append(url)
    
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        files = [os.path.join(dl_dir, f) for f in os.listdir(dl_dir) if os.path.isfile(os.path.join(dl_dir, f))]
        
        # Direct URL download fallback if yt-dlp didn't catch it
        if not files and url.startswith("http"):
            import requests
            parsed_path = urllib.parse.urlparse(url).path
            fname = os.path.basename(parsed_path) or f"download_{int(time.time())}"
            fpath = os.path.join(dl_dir, fname)
            try:
                r = requests.get(url, stream=True, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    with open(fpath, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            f.write(chunk)
                    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
                        files = [fpath]
            except Exception:
                pass

        if not files:
            return f"خطا در دانلود مدیا: فایلی دریافت نشد (احتمالاً حجم بالای ۴۸ مگابایت یا عدم دسترسی به لینک). خروجی: {proc.stderr[:160]}"
            
        file_to_send = max(files, key=os.path.getsize)
        file_size_mb = round(os.path.getsize(file_to_send) / (1024 * 1024), 2)
        base_name = os.path.basename(file_to_send)
        ext = os.path.splitext(base_name)[1].lower()
        
        edit_bale_message(chat_id, msg_id, f"📤 **در حال آپلود و ارسال مدیا ({file_size_mb} MB) به چت...**", bale_token)
        
        method = "sendDocument"
        field = "document"
        if ext in [".mp4", ".mov", ".mkv", ".webm"] and media_type != "audio":
            method = "sendVideo"
            field = "video"
        elif ext in [".mp3", ".m4a", ".wav", ".ogg"] or media_type == "audio":
            method = "sendAudio"
            field = "audio"
        elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
            method = "sendPhoto"
            field = "photo"
            
        caption = f"🎬 **{base_name}**\n📦 حجم: {file_size_mb}MB\n🤖 دریافت شده توسط دستیار Mr.3D"
        res = send_bale_file(method, chat_id, file_to_send, caption, field, bale_token)
        if not (res and res.get("ok")):
            res = send_bale_file("sendDocument", chat_id, file_to_send, caption, "document", bale_token)
            
        if res and res.get("ok"):
            return f"مدیا با نام '{base_name}' و حجم {file_size_mb}MB با موفقیت دانلود شد و به صورت مستقیم برای کاربر در چت بله ارسال گردید."
        return f"فایل ({file_size_mb}MB) دانلود شد ولی ارسال به بله با خطا مواجه شد."
    except subprocess.TimeoutExpired:
        return "زمان مجاز دانلود به پایان رسید (بیش از ۳ دقیقه)."
    except Exception as e:
        return f"خطا در اجرای دانلود مدیا: {e}"

def run_generate_image(prompt: str, chat_id: str, bale_token: str, msg_id: str) -> str:
    print(f"[Agent Tool] Generating image: {prompt}", flush=True)
    edit_bale_message(chat_id, msg_id, f"🎨 **در حال طراحی و تولید تصویر با هوش مصنوعی...**\n`{prompt[:60]}`", bale_token)
    try:
        import requests
        enc_prompt = urllib.parse.quote(prompt[:400])
        seed = secrets.randbelow(1000000)
        img_url = f"https://image.pollinations.ai/prompt/{enc_prompt}?width=1024&height=1024&nologo=true&seed={seed}&model=flux"
        resp = requests.get(img_url, timeout=75)
        if resp.status_code == 200 and len(resp.content) > 1000:
            img_path = f"/tmp/gen_{int(time.time())}_{seed}.jpg"
            with open(img_path, "wb") as f:
                f.write(resp.content)
            caption = f"🎨 **تصویر تولید شده با هوش مصنوعی**\nپرامپت: {prompt}\n🤖 توسط دستیار Mr.3D"
            res = send_bale_file("sendPhoto", chat_id, img_path, caption, "photo", bale_token)
            if res and res.get("ok"):
                return f"تصویر با پرامپت '{prompt}' با موفقیت تولید شد و مستقیماً در چت بله ارسال گردید."
            return f"تصویر تولید شد اما ارسال به چت بله ناموفق بود: {res}"
        return f"سرویس تولید تصویر خطا داد (HTTP {resp.status_code})."
    except Exception as e:
        return f"خطا در تولید تصویر: {e}"

def run_take_screenshot(url: str, chat_id: str, bale_token: str, msg_id: str) -> str:
    print(f"[Agent Tool] Taking screenshot: {url}", flush=True)
    edit_bale_message(chat_id, msg_id, f"📸 **در حال تصویربرداری از وب‌سایت...**\n`{url}`", bale_token)
    shot_path = f"/tmp/shot_{int(time.time())}.png"
    success = False
    
    # 1. Try headless Chrome
    try:
        subprocess.run([
            "google-chrome",
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--screenshot={shot_path}",
            "--window-size=1280,800",
            url
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
        if os.path.exists(shot_path) and os.path.getsize(shot_path) > 1000:
            success = True
    except Exception:
        pass
        
    # 2. Fallback to screenshot API
    if not success:
        try:
            import requests
            api_url = f"https://image.thum.io/get/width/1200/crop/800/{url}"
            r = requests.get(api_url, timeout=30)
            if r.status_code == 200 and len(r.content) > 1000:
                with open(shot_path, "wb") as f:
                    f.write(r.content)
                success = True
        except Exception:
            pass
            
    if success and os.path.exists(shot_path):
        caption = f"📸 **اسکرین‌شات از صفحه وب:**\n{url}\n🤖 توسط دستیار Mr.3D"
        res = send_bale_file("sendPhoto", chat_id, shot_path, caption, "photo", bale_token)
        if res and res.get("ok"):
            return f"اسکرین‌شات از وب‌سایت {url} با موفقیت ثبت شد و در چت ارسال گردید."
        return f"اسکرین‌شات ثبت شد ولی ارسال در بله ناموفق بود: {res}"
    return f"امکان ثبت اسکرین‌شات از آدرس {url} فراهم نشد."

def run_manage_workspace(action: str, filename: str, content: str, chat_id: str) -> str:
    ws_dir = os.path.join(os.getcwd(), "agent_workspace", str(chat_id))
    os.makedirs(ws_dir, exist_ok=True)
    safe_fn = os.path.basename(filename) if filename else ""
    target_path = os.path.join(ws_dir, safe_fn) if safe_fn else ""
    
    if action == "list":
        if not os.path.exists(ws_dir):
            return "فضای کاری شما در حال حاضر خالی است."
        files = os.listdir(ws_dir)
        if not files:
            return "هیچ فایلی در فضای کاری شما در ریپازیتوری وجود ندارد."
        items = []
        for f in files:
            fp = os.path.join(ws_dir, f)
            sz = round(os.path.getsize(fp) / 1024, 1)
            items.append(f"• `{f}` ({sz} KB)")
        return "📁 **فایل‌های فضای کاری ذخیره‌شده شما در ریپازیتوری:**\n" + "\n".join(items)
        
    elif action == "write":
        if not safe_fn:
            return "خطا: نام فایل برای ذخیره الزامی است."
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content or "")
        try:
            subprocess.run(["git", "config", "user.name", "BaleBot Agent"], check=False)
            subprocess.run(["git", "config", "user.email", "agent@balebot.local"], check=False)
            subprocess.run(["git", "add", "agent_workspace/"], check=False)
            subprocess.run(["git", "commit", "-m", f"Agent save: {safe_fn} for chat {chat_id}"], check=False)
            subprocess.run(["git", "push"], check=False)
        except Exception as e:
            print(f"[Git Sync] {e}", file=sys.stderr)
        return f"فایل `{safe_fn}` با موفقیت در فضای کاری دائمی ریپازیتوری ذخیره گردید."
        
    elif action == "read":
        if not safe_fn or not os.path.exists(target_path):
            return f"فایل `{safe_fn}` در فضای کاری شما پیدا نشد."
        try:
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(4000)
            return f"محتوای فایل `{safe_fn}`:\n\n{data}"
        except Exception as e:
            return f"خطا در خواندن فایل: {e}"
            
    elif action == "delete":
        if safe_fn and os.path.exists(target_path):
            os.remove(target_path)
            try:
                subprocess.run(["git", "add", "-u", "agent_workspace/"], check=False)
                subprocess.run(["git", "commit", "-m", f"Agent delete: {safe_fn} for chat {chat_id}"], check=False)
                subprocess.run(["git", "push"], check=False)
            except Exception:
                pass
            return f"فایل `{safe_fn}` با موفقیت از فضای کاری حذف شد."
        return f"فایل `{safe_fn}` یافت نشد."
        
    return "دستور نامعتبر در مدیریت فضای کاری."

def run_upload_to_gdrive(file_path: str, drive_folder_id: str, chat_id: str) -> str:
    gdrive_creds = os.environ.get("GDRIVE_CREDENTIALS", "").strip()
    if not gdrive_creds:
        return (
            "⚠️ سرویس Google Drive در تنظیمات GitHub Secrets پیکربندی نشده است.\n"
            "برای فعال‌سازی کافیست کلید Service Account گوگل را در سکرت `GDRIVE_CREDENTIALS` قرار دهید.\n"
            "در حال حاضر فایل‌ها مستقیماً در چت بله تحویل کاربر داده می‌شوند."
        )
    ws_dir = os.path.join(os.getcwd(), "agent_workspace", str(chat_id))
    candidates = [
        file_path,
        os.path.join(ws_dir, os.path.basename(file_path)),
        os.path.join("/tmp", os.path.basename(file_path))
    ]
    target = None
    for c in candidates:
        if os.path.exists(c) and os.path.isfile(c):
            target = c
            break
    if not target:
        return f"فایل مورد نظر برای آپلود در گوگل درایو یافت نشد: {file_path}"
        
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        
        creds_dict = json.loads(gdrive_creds)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/drive.file']
        )
        service = build('drive', 'v3', credentials=creds)
        file_metadata = {'name': os.path.basename(target)}
        if drive_folder_id:
            file_metadata['parents'] = [drive_folder_id]
            
        media = MediaFileUpload(target, resumable=True)
        uploaded = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
        
        service.permissions().create(
            fileId=uploaded.get('id'),
            body={'role': 'reader', 'type': 'anyone'}
        ).execute()
        
        link = uploaded.get('webViewLink')
        return f"فایل با موفقیت در Google Drive آپلود شد!\n🔗 لینک اشتراک‌گذاری:\n{link}"
    except Exception as e:
        return f"خطا در ارتباط با گوگل درایو: {e}"

# ──────────────── Tool Definitions for OpenCode Zen ────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read file contents from disk or repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Path to file"}
                },
                "required": ["filePath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the live web for current facts, news, documentation, or real-time info.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": "Fetch, scrape, and extract text and markdown content from a given webpage URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Webpage URL to read and parse"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "download_media",
            "description": "Download video or audio from YouTube, Instagram, TikTok, Twitter/X, SoundCloud, or direct URLs, and send it directly to the user in Bale chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Video/Audio/Post URL to download"},
                    "media_type": {"type": "string", "enum": ["video", "audio", "auto"], "description": "Type of media to download. Default is auto."}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an AI image from a descriptive prompt and send the photo directly to the user in Bale chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image generation prompt in English for highest visual quality"}
                },
                "required": ["prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Capture a live full-page screenshot of a website URL and send it as a photo to the user in Bale chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL of the webpage to capture"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_workspace",
            "description": "Manage files in the user's persistent workspace in the repository across sessions. Read, write, list, or delete notes, code, and files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write", "list", "delete"], "description": "Action to perform"},
                    "filename": {"type": "string", "description": "Name of the file (required for read, write, delete)"},
                    "content": {"type": "string", "description": "Text content to write (required for write)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "upload_to_gdrive",
            "description": "Upload a file or document to Google Drive and obtain a shareable download link.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path or filename in workspace to upload"},
                    "drive_folder_id": {"type": "string", "description": "Optional Google Drive folder ID"}
                },
                "required": ["file_path"]
            }
        }
    }
]

# ──────────────── OpenCode SSE Caller ────────────────
def call_opencode_stream(messages, model, api_key, tools_override=None):
    pace_request()
    
    auth_tokens_to_try = []
    if api_key and api_key.strip():
        auth_tokens_to_try.append(api_key.strip())
    auth_tokens_to_try.append("public")
    
    url = "https://opencode.ai/zen/v1/chat/completions"
    current_tools = TOOLS if tools_override is None else tools_override
    
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
        "max_tokens": 2048
    }
    if current_tools:
        payload["tools"] = current_tools

    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    delays = [2, 5, 10]
    
    for token_candidate in auth_tokens_to_try:
        is_key = (token_candidate != "public")
        for attempt, delay in enumerate([0] + delays):
            if delay > 0:
                time.sleep(delay)
                pace_request()
                
            headers = {
                "User-Agent": "opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
                "x-opencode-client": "cli",
                "x-opencode-project": "global",
                "Accept": "*/*",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token_candidate}",
                "x-opencode-session": gen_opencode_session_id(),
                "x-opencode-request": gen_opencode_request_id(),
            }
            
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                resp = urllib.request.urlopen(req, timeout=45)
                full_content = ""
                full_reasoning = ""
                tool_calls_map = {}
                
                for line in resp:
                    line_str = line.decode('utf-8', errors='ignore').strip()
                    if not line_str.startswith('data:'):
                        continue
                    line_data = line_str[5:].strip()
                    if line_data == '[DONE]':
                        break
                    try:
                        chunk = json.loads(line_data)
                        choices = chunk.get('choices')
                        if not choices:
                            continue
                        delta = choices[0].get('delta', {})
                        if 'content' in delta and delta['content']:
                            full_content += delta['content']
                        if 'reasoning' in delta and delta['reasoning']:
                            full_reasoning += delta['reasoning']
                        if 'tool_calls' in delta and delta['tool_calls']:
                            for tc in delta['tool_calls']:
                                idx = tc.get('index', 0)
                                if idx not in tool_calls_map:
                                    tool_calls_map[idx] = {
                                        'id': tc.get('id', ''),
                                        'name': tc.get('function', {}).get('name', ''),
                                        'arguments': ''
                                    }
                                if tc.get('id'):
                                    tool_calls_map[idx]['id'] = tc['id']
                                if tc.get('function', {}).get('name'):
                                    tool_calls_map[idx]['name'] = tc['function']['name']
                                if tc.get('function', {}).get('arguments'):
                                    tool_calls_map[idx]['arguments'] += tc['function']['arguments']
                    except Exception:
                        continue
                
                if not full_content.strip() and full_reasoning.strip() and not tool_calls_map:
                    full_content = full_reasoning.strip()
                
                tool_calls = list(tool_calls_map.values()) if tool_calls_map else None
                return {
                    "ok": True,
                    "content": full_content,
                    "reasoning": full_reasoning,
                    "tool_calls": tool_calls
                }
            except urllib.error.HTTPError as e:
                status = e.code
                err_body = e.read().decode('utf-8', errors='ignore')
                print(f"[OpenCode Error] Status {status} (token={'key' if is_key else 'public'}): {err_body[:200]}", file=sys.stderr, flush=True)
                if status in (401, 403):
                    break
                elif status == 429:
                    continue
                else:
                    if attempt < len(delays):
                        continue
            except Exception as e:
                print(f"[OpenCode Network Error] {e}", file=sys.stderr, flush=True)
                if attempt < len(delays):
                    continue
                    
    return {"ok": False, "error": "OpenCode API request failed on all attempts."}

# ──────────────── Job Processor ────────────────
def process_job(job_data, bale_token, opencode_key, worker_url):
    question = job_data.get("question", "").strip()
    model = job_data.get("model", "").strip() or "mimo-v2.6-flash-free"
    chat_id = job_data.get("chat_id", "").strip()
    msg_id = job_data.get("msg_id", "").strip()
    group_chat_id = job_data.get("group_chat_id", "").strip()
    topic_id = job_data.get("topic_id", "").strip() or "main"
    topic_title = job_data.get("topic_title", "").strip() or "گفتگوی عمومی"
    history = job_data.get("history") or []

    if not question:
        return

    print(f"\n[Agent Processing] Chat: {chat_id}, Topic: {topic_title} ({topic_id}), Model: {model}, Question: {question[:80]}", flush=True)

    sys_prompt = (
        "You are 'Mr.3D Super Agent', an advanced autonomous AI assistant in the Bale messenger app.\n"
        "You have powerful real-world tools that you can execute autonomously:\n"
        "1. 'web_search': Search the live web for current facts, news, documentation, or real-time information.\n"
        "2. 'read_webpage': Fetch, scrape, and read the full text/content of any webpage or article URL.\n"
        "3. 'download_media': Download media (YouTube, Instagram reels/posts, TikTok, Twitter/X, SoundCloud, or direct URLs) and send the video/audio file directly to the user in their Bale chat!\n"
        "4. 'generate_image': Generate high quality AI images from descriptive prompts and immediately send the photo to the user in Bale.\n"
        "5. 'take_screenshot': Capture a full screenshot of any website or URL and send the photo directly to the user in Bale.\n"
        "6. 'manage_workspace': Save, read, list, or delete persistent notes, code, documents, or data files in the user's permanent workspace in the repository across sessions.\n"
        "7. 'upload_to_gdrive': Upload files to Google Drive and provide a shareable download link.\n\n"
        "Always respond in fluent, natural Persian (Farsi) unless the user asks in another language.\n"
        "When the user asks you to download a video, generate an image, take a screenshot, save a file, or search the web, execute the appropriate tool proactively!\n"
        "After tools run, formulate a complete, friendly Persian response summarizing the action and findings."
    )

    messages = [{"role": "system", "content": sys_prompt}]
    if history:
        for turn in history:
            if isinstance(turn, dict) and turn.get("role") and turn.get("content"):
                messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    MAX_AGENT_TURNS = 4
    final_answer = ""
    tool_results_log = []

    for turn in range(MAX_AGENT_TURNS):
        res = call_opencode_stream(messages, model, opencode_key)
        if not res.get("ok"):
            err_msg = res.get("error", "خطا در برقراری ارتباط با سرور مدل هوش مصنوعی.")
            print(f"[Turn {turn+1} Error] {err_msg}", file=sys.stderr)
            break

        tool_calls = res.get("tool_calls")
        content = res.get("content", "").strip()
        reasoning = res.get("reasoning", "").strip()

        if tool_calls and turn < MAX_AGENT_TURNS - 1:
            print(f"[Turn {turn+1}] Model requested {len(tool_calls)} tool call(s)", flush=True)
            asst_msg = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": tc["id"] or f"call_{i}",
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]}
                    }
                    for i, tc in enumerate(tool_calls)
                ]
            }
            messages.append(asst_msg)

            for i, tc in enumerate(tool_calls):
                tc_name = tc.get("name")
                tc_args_str = tc.get("arguments", "{}")
                tc_id = tc.get("id") or f"call_{i}"
                tc_args = {}
                try:
                    tc_args = json.loads(tc_args_str)
                except Exception:
                    pass

                tool_output = ""
                if tc_name == "web_search":
                    query = tc_args.get("query") or question
                    edit_bale_message(chat_id, msg_id, f"🔍 **در حال جستجوی وب برای:**\n`{query}`...", bale_token)
                    tool_output = run_web_search(query)
                    tool_results_log.append(f"جستجو برای '{query}':\n{tool_output}")
                    
                elif tc_name == "read_webpage":
                    url = tc_args.get("url") or ""
                    edit_bale_message(chat_id, msg_id, f"📄 **در حال خواندن صفحه وب:**\n`{url}`...", bale_token)
                    tool_output = run_read_webpage(url)
                    tool_results_log.append(f"محتوای صفحه '{url}':\n{tool_output[:500]}")
                    
                elif tc_name == "download_media":
                    url = tc_args.get("url") or ""
                    media_type = tc_args.get("media_type") or "auto"
                    tool_output = run_download_media(url, media_type, chat_id, bale_token, msg_id)
                    tool_results_log.append(f"دانلود مدیا: {tool_output}")
                    
                elif tc_name == "generate_image":
                    prompt = tc_args.get("prompt") or question
                    tool_output = run_generate_image(prompt, chat_id, bale_token, msg_id)
                    tool_results_log.append(f"تولید تصویر: {tool_output}")
                    
                elif tc_name == "take_screenshot":
                    url = tc_args.get("url") or ""
                    tool_output = run_take_screenshot(url, chat_id, bale_token, msg_id)
                    tool_results_log.append(f"اسکرین‌شات: {tool_output}")
                    
                elif tc_name == "manage_workspace":
                    action = tc_args.get("action") or "list"
                    fn = tc_args.get("filename") or ""
                    cnt = tc_args.get("content") or ""
                    tool_output = run_manage_workspace(action, fn, cnt, chat_id)
                    tool_results_log.append(f"فضای کاری ({action}): {tool_output}")
                    
                elif tc_name == "upload_to_gdrive":
                    fp = tc_args.get("file_path") or ""
                    fid = tc_args.get("drive_folder_id") or ""
                    tool_output = run_upload_to_gdrive(fp, fid, chat_id)
                    tool_results_log.append(f"گوگل درایو: {tool_output}")
                    
                elif tc_name == "read":
                    fp = tc_args.get("filePath") or tc_args.get("path") or ""
                    tool_output = run_read_file(fp)
                    tool_results_log.append(f"خواندن فایل: {tool_output}")
                    
                else:
                    tool_output = "ابزار با موفقیت اجرا شد."

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tc_name or "unknown",
                    "content": tool_output
                })

            if turn == MAX_AGENT_TURNS - 2:
                edit_bale_message(chat_id, msg_id, "✍️ **در حال نگارش پاسخ کامل بر اساس نتایج...**", bale_token)
                messages.append({
                    "role": "user",
                    "content": "با توجه به نتایج تمام ابزارهای اجرا شده، لطفاً پاسخ نهایی، کامل و شیوا را به زبان فارسی ارائه بده."
                })
        else:
            final_answer = content.strip() or reasoning.strip()
            break

    # If final answer is still empty but tools ran, synthesize from results
    if not final_answer and tool_results_log:
        print("[Agent Fallback] Synthesizing answer from tool results...", flush=True)
        edit_bale_message(chat_id, msg_id, "✍️ **در حال جمع‌بندی نهایی...**", bale_token)
        synth_msgs = [
            {"role": "system", "content": "You are a helpful Persian AI assistant named Mr.3D Super Agent. Answer the user comprehensively in Persian based on the actions taken and results."},
            {"role": "user", "content": f"درخواست کاربر: {question}\n\nنتایج کارهای انجام شده توسط ابزارها:\n" + "\n\n".join(tool_results_log[:6]) + "\n\nپاسخ نهایی را به صورت کامل و دوستانه به زبان فارسی بنویس."}
        ]
        synth_res = call_opencode_stream(synth_msgs, "nemotron-3.5-lightning-free", opencode_key)
        if synth_res.get("ok"):
            final_answer = synth_res.get("content", "").strip() or synth_res.get("reasoning", "").strip()

    # Emergency fallback retry if still empty
    if not final_answer:
        em_res = call_opencode_stream([
            {"role": "system", "content": "You are a helpful Persian AI assistant."},
            {"role": "user", "content": question}
        ], "nemotron-3.5-lightning-free", opencode_key)
        if em_res.get("ok"):
            final_answer = em_res.get("content", "").strip()

    if not final_answer:
        edit_bale_message(chat_id, msg_id, "❌ متأسفانه دریافت پاسخ از سرور هوش مصنوعی با خطا مواجه شد. لطفاً دوباره پیام خود را بفرستید.", bale_token)
        return

    # Clean leaked reasoning headers if any
    hdr = final_answer.rfind('**Answer**')
    if hdr != -1 and re.search(r'\*\*Reasoning|Reasoning and information|Reasoning used', final_answer[:hdr], re.IGNORECASE):
        final_answer = final_answer[hdr + len('**Answer**'):].strip()

    full_text = f"🤖 **پاسخ دستیار Mr.3D** ({model}):\n\n{final_answer}"

    # Deliver to user (chunked if > 3800 chars)
    if len(full_text) <= 3800:
        edit_bale_message(chat_id, msg_id, full_text, bale_token)
    else:
        chunks = [full_text[i:i+3500] for i in range(0, len(full_text), 3500)]
        edit_bale_message(chat_id, msg_id, chunks[0], bale_token)
        for chunk in chunks[1:]:
            send_bale_message(chat_id, chunk, bale_token)

    # Optional group chat notification
    if group_chat_id and group_chat_id != "0":
        send_bale_message(group_chat_id, f"🤖 **AI Answer**\n\n{final_answer}", bale_token)

    # Update conversation history on Cloudflare Worker with topic_id
    if worker_url and worker_url.startswith("http"):
        try:
            update_url = f"{worker_url.rstrip('/')}/ai-history-update"
            update_body = json.dumps({
                "chat_id": chat_id,
                "topic_id": topic_id,
                "user_message": question,
                "assistant_message": final_answer
            }, ensure_ascii=False).encode('utf-8')
            ureq = urllib.request.Request(update_url, data=update_body, headers={"Content-Type": "application/json", "User-Agent": "CFWorker-Agent/1.0"}, method="POST")
            with urllib.request.urlopen(ureq, timeout=10) as uresp:
                print(f"[History Updated] Status: {uresp.status} for topic {topic_id}", flush=True)
        except Exception as e:
            print(f"[History Update Note] Could not update worker history: {e}", file=sys.stderr, flush=True)

    print(f"[Job Finished Successfully for Chat {chat_id}, Topic {topic_id}]", flush=True)

# ──────────────── Main Runner & Daemon Loop ────────────────
def main():
    bale_token = os.environ.get("BALE_BOT_TOKEN", "").strip()
    opencode_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    worker_url = os.environ.get("WORKER_URL", "").strip()
    initial_job = None

    # Read from event payload if present
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                event_data = json.load(f)
            payload = event_data.get("client_payload", {})
            if payload.get("question"):
                initial_job = {
                    "question": str(payload.get("question", "")).strip(),
                    "model": str(payload.get("model", "")).strip() or "mimo-v2.6-flash-free",
                    "chat_id": str(payload.get("chat_id", "")).strip(),
                    "msg_id": str(payload.get("msg_id", "")).strip(),
                    "group_chat_id": str(payload.get("group_chat_id", "")).strip(),
                    "topic_id": str(payload.get("topic_id", "")).strip() or "main",
                    "topic_title": str(payload.get("topic_title", "")).strip() or "گفتگوی عمومی",
                    "history": payload.get("history") or []
                }
            if payload.get("worker_url"):
                worker_url = payload["worker_url"].strip()
        except Exception as e:
            print(f"[Event Read Warning] {e}", file=sys.stderr)

    if not initial_job and os.environ.get("QUESTION"):
        initial_job = {
            "question": os.environ.get("QUESTION", "").strip(),
            "model": os.environ.get("MODEL", "").strip() or "mimo-v2.6-flash-free",
            "chat_id": os.environ.get("CHAT_ID", "").strip(),
            "msg_id": os.environ.get("MSG_ID", "").strip(),
            "group_chat_id": os.environ.get("GROUP_CHAT_ID", "").strip(),
            "topic_id": os.environ.get("TOPIC_ID", "").strip() or "main",
            "topic_title": os.environ.get("TOPIC_TITLE", "").strip() or "گفتگوی عمومی",
            "history": []
        }

    # Process initial job if provided
    if initial_job and initial_job.get("question"):
        print("[Runner] Processing initial dispatch job...", flush=True)
        try:
            process_job(initial_job, bale_token, opencode_key, worker_url)
        except Exception as e:
            print(f"[Initial Job Error] {e}", file=sys.stderr, flush=True)

    # Now enter the long-running runner daemon loop (up to 5.3 hours)
    if not worker_url:
        print("[Runner] No WORKER_URL specified, exiting.", flush=True)
        return

    print(f"[Runner Daemon Started] Polling {worker_url}/ai-poll for incoming chats (5.3 hour lifespan)...", flush=True)
    start_time = time.time()
    MAX_LIFESPAN = 5.3 * 3600  # 5 hours and 18 minutes

    poll_fail_count = 0
    last_hb_time = 0.0
    while time.time() - start_time < MAX_LIFESPAN:
        try:
            now = time.time()
            send_hb = (now - last_hb_time >= 60.0)
            poll_url = f"{worker_url.rstrip('/')}/ai-poll" + ("?hb=1" if send_hb else "")
            req = urllib.request.Request(poll_url, headers={"User-Agent": "GitHubActions-AIRunner/1.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if send_hb:
                    last_hb_time = now
                poll_fail_count = 0
                job = data.get("job")
                if job and isinstance(job, dict) and job.get("question"):
                    process_job(job, bale_token, opencode_key, worker_url)
                    continue
        except Exception as e:
            poll_fail_count += 1
            if poll_fail_count % 30 == 0:
                print(f"[Poll Notice] Repeated poll network error: {e}", file=sys.stderr, flush=True)
            time.sleep(3.0)
            continue

        time.sleep(2.5)

    print("[Runner Daemon Finished] Lifespan limit reached. Exiting cleanly to allow fresh runner next time.", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL AGENT ERROR] {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
