#!/usr/bin/env python3
"""
Autonomous AI Agent for Bale Messenger (GitHub Actions Runner)
- Lightweight autonomous agent (Claude Code / Cursor style)
- Real tool execution: bash commands, python execution, read, write, edit, glob, grep
- Dual Workspace Architecture:
    * Ephemeral Runner Workspace: /tmp/agent_scratch (temporary scripts, tests, downloads)
    * Persistent Workspace: agent_workspace/{chat_id}/ (Git-committed & pushed to repository)
- 200k Context limit management with automated compression
- 5-layer anti-rate-limit OpenCode Zen client emulation (HTTP/2, session persistence, CLI schema)
- Multi-turn autonomous tool loop (up to 10 turns)
"""

import os
import sys
import json
import time
import secrets
import threading
import urllib.request
import urllib.parse
import subprocess
import glob as glob_module
import re
import httpx

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

# ──────────────── Git Integration Helper ────────────────
def ensure_git_config():
    try:
        subprocess.run(["git", "config", "user.name", "Mr3D-Agent"], check=False)
        subprocess.run(["git", "config", "user.email", "agent@bale.bot"], check=False)
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip() or "Amiro3D/BaleTubeAction"
        if token and repo:
            remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
            subprocess.run(["git", "remote", "set-url", "origin", remote_url], check=False)
    except Exception:
        pass

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

def run_bash(command: str, timeout: int = 60) -> str:
    """Executes arbitrary bash commands or scripts on the Linux runner."""
    print(f"[Agent Tool: bash] {command[:100]}", flush=True)
    scratch_dir = "/tmp/agent_scratch"
    os.makedirs(scratch_dir, exist_ok=True)
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=min(max(timeout, 5), 180),
            cwd=os.getcwd()
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        out = [f"Exit code: {proc.returncode}"]
        if stdout:
            if len(stdout) > 3500:
                stdout = stdout[:3500] + "\n... [STDOUT TRUNCATED]"
            out.append(f"STDOUT:\n{stdout}")
        if stderr:
            if len(stderr) > 2000:
                stderr = stderr[:2000] + "\n... [STDERR TRUNCATED]"
            out.append(f"STDERR:\n{stderr}")
        if not stdout and not stderr:
            out.append("(Command succeeded with no output)")
        return "\n".join(out)
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout} seconds."
    except Exception as e:
        return f"Error executing bash: {e}"

def run_read_file(filepath: str, offset: int = 1, limit: int = 300) -> str:
    """Reads file content from disk with optional line ranges."""
    print(f"[Agent Tool: read] {filepath} (lines {offset}..{offset+limit})", flush=True)
    try:
        target = filepath
        if not os.path.exists(target):
            alt = os.path.join(os.getcwd(), filepath)
            if os.path.exists(alt):
                target = alt
            else:
                return f"Error: File '{filepath}' does not exist."
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total_lines = len(lines)
        start_idx = max(1, offset) - 1
        end_idx = min(total_lines, start_idx + max(1, limit))
        selected_lines = lines[start_idx:end_idx]
        formatted = "".join(f"{i+start_idx+1}: {line}" for i, line in enumerate(selected_lines))
        return f"File: {filepath} ({total_lines} total lines, showing lines {start_idx+1}-{end_idx}):\n{formatted}"
    except Exception as e:
        return f"Error reading file '{filepath}': {e}"

def run_write_file(filepath: str, content: str) -> str:
    """Creates or overwrites a file with content."""
    print(f"[Agent Tool: write] {filepath} ({len(content)} chars)", flush=True)
    try:
        parent = os.path.dirname(filepath)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"File '{filepath}' written successfully ({len(content)} characters)."
    except Exception as e:
        return f"Error writing file '{filepath}': {e}"

def run_edit_file(filepath: str, old_string: str, new_string: str) -> str:
    """Replaces occurrences of old_string with new_string in a file."""
    print(f"[Agent Tool: edit] {filepath}", flush=True)
    try:
        target = filepath
        if not os.path.exists(target):
            alt = os.path.join(os.getcwd(), filepath)
            if os.path.exists(alt):
                target = alt
            else:
                return f"Error: File '{filepath}' not found."
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()
        if old_string not in content:
            return f"Error: 'old_string' was not found in '{filepath}'. Match exact whitespace and characters."
        occurrences = content.count(old_string)
        updated = content.replace(old_string, new_string, 1)
        with open(target, "w", encoding="utf-8") as f:
            f.write(updated)
        return f"File '{filepath}' edited successfully (replaced 1 occurrence; {occurrences} found)."
    except Exception as e:
        return f"Error editing file '{filepath}': {e}"

def run_glob_files(pattern: str, search_path: str = ".") -> str:
    """Finds files matching pattern."""
    print(f"[Agent Tool: glob] {pattern} in {search_path}", flush=True)
    try:
        full_pattern = os.path.join(search_path, pattern) if search_path != "." else pattern
        matches = glob_module.glob(full_pattern, recursive=True)
        if not matches:
            return f"No files matched pattern '{pattern}'."
        matches = matches[:80]
        return f"Found {len(matches)} matching files:\n" + "\n".join(f"• {m}" for m in matches)
    except Exception as e:
        return f"Error in glob: {e}"

def run_grep(pattern: str, search_path: str = ".") -> str:
    """Searches for pattern across files."""
    print(f"[Agent Tool: grep] {pattern} in {search_path}", flush=True)
    try:
        cmd = ["grep", "-rnI", "--exclude-dir=.git", pattern, search_path]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        stdout = proc.stdout.strip()
        if not stdout:
            return f"No matches found for '{pattern}' in {search_path}."
        lines = stdout.splitlines()[:60]
        return f"Matches found ({len(lines)} shown):\n" + "\n".join(lines)
    except Exception as e:
        return f"Error running grep: {e}"

def run_web_search(query: str) -> str:
    print(f"[Agent Tool: web_search] {query}", flush=True)
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
    print(f"[Agent Tool: read_webpage] {url}", flush=True)
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
        if len(text) > 4000:
            text = text[:4000] + "..."
        return f"عنوان صفحه: {title}\n\nمحتوای متنی صفحه:\n{text}"
    except Exception as e:
        return f"خطا در خواندن صفحه وب: {e}"

def run_download_media(url: str, media_type: str, chat_id: str, bale_token: str, msg_id: str) -> str:
    print(f"[Agent Tool: download_media] {url} (type={media_type})", flush=True)
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
        
        # Direct URL download fallback
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
            return f"خطا در دانلود مدیا: فایلی دریافت نشد (حجم بالای ۴۸ مگابایت یا عدم دسترسی به لینک). خروجی: {proc.stderr[:160]}"
            
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
            
        caption = f"🎬 **{base_name}**\n📦 حجم: {file_size_mb}MB\n🤖 دریافت شده توسط دستیار هوشمند Mr.3D"
        res = send_bale_file(method, chat_id, file_to_send, caption, field, bale_token)
        if not (res and res.get("ok")):
            res = send_bale_file("sendDocument", chat_id, file_to_send, caption, "document", bale_token)
            
        if res and res.get("ok"):
            return f"مدیا با نام '{base_name}' و حجم {file_size_mb}MB با موفقیت دانلود شد و مستقیماً برای کاربر ارسال شد."
        return f"فایل ({file_size_mb}MB) دانلود شد ولی ارسال به بله با خطا مواجه شد."
    except subprocess.TimeoutExpired:
        return "زمان مجاز دانلود به پایان رسید (بیش از ۳ دقیقه)."
    except Exception as e:
        return f"خطا در اجرای دانلود مدیا: {e}"

def run_generate_image(prompt: str, chat_id: str, bale_token: str, msg_id: str) -> str:
    print(f"[Agent Tool: generate_image] {prompt}", flush=True)
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
    print(f"[Agent Tool: take_screenshot] {url}", flush=True)
    edit_bale_message(chat_id, msg_id, f"📸 **در حال تصویربرداری از وب‌سایت...**\n`{url}`", bale_token)
    shot_path = f"/tmp/shot_{int(time.time())}.png"
    success = False
    
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
        caption = f"📸 **اسکرین‌شات از وب‌سایت:**\n{url}\n🤖 توسط دستیار Mr.3D"
        res = send_bale_file("sendPhoto", chat_id, shot_path, caption, "photo", bale_token)
        if res and res.get("ok"):
            return f"اسکرین‌شات از وب‌سایت {url} با موفقیت ثبت شد و در چت ارسال گردید."
        return f"اسکرین‌شات ثبت شد ولی ارسال در بله ناموفق بود: {res}"
    return f"امکان ثبت اسکرین‌شات از آدرس {url} فراهم نشد."

def run_manage_workspace(action: str, filename: str, content: str, chat_id: str) -> str:
    """Manages persistent files in agent_workspace/{chat_id}/ with automatic Git commit & push."""
    ws_dir = os.path.join(os.getcwd(), "agent_workspace", str(chat_id))
    os.makedirs(ws_dir, exist_ok=True)
    safe_fn = os.path.basename(filename) if filename else ""
    target_path = os.path.join(ws_dir, safe_fn) if safe_fn else ""
    
    ensure_git_config()
    
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
            subprocess.run(["git", "add", "agent_workspace/"], check=False)
            subprocess.run(["git", "commit", "-m", f"Agent workspace save: {safe_fn} for chat {chat_id}"], check=False)
            subprocess.run(["git", "push"], check=False)
        except Exception as e:
            print(f"[Git Sync Warning] {e}", file=sys.stderr)
        return f"فایل `{safe_fn}` با موفقیت در فضای کاری دائمی ریپازیتوری ذخیره و در گیت کامیت و پوش شد."
        
    elif action == "read":
        if not safe_fn or not os.path.exists(target_path):
            return f"فایل `{safe_fn}` در فضای کاری شما پیدا نشد."
        try:
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(8000)
            return f"محتوای فایل `{safe_fn}`:\n\n{data}"
        except Exception as e:
            return f"خطا در خواندن فایل: {e}"
            
    elif action == "delete":
        if safe_fn and os.path.exists(target_path):
            os.remove(target_path)
            try:
                subprocess.run(["git", "add", "-u", "agent_workspace/"], check=False)
                subprocess.run(["git", "commit", "-m", f"Agent workspace delete: {safe_fn} for chat {chat_id}"], check=False)
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
            "⚠️ سرویس Google Drive در سکرت‌های ریپازیتوری تنظیم نشده است.\n"
            "فایل مورد نظر در خروجی چت یا در ریپازیتوری در دسترس است."
        )
    ws_dir = os.path.join(os.getcwd(), "agent_workspace", str(chat_id))
    candidates = [
        file_path,
        os.path.join(ws_dir, os.path.basename(file_path)),
        os.path.join("/tmp/agent_scratch", os.path.basename(file_path)),
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
            "name": "bash",
            "description": "Execute any bash shell command on the Linux runner (e.g., run Python scripts, compile, execute pip, git commands, inspect environment, test code, run tools). Scratch space is available in /tmp/agent_scratch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command or script to execute in bash."},
                    "timeout": {"type": "integer", "description": "Command timeout in seconds (default 60)."}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read contents of any file from disk or repository. Supports line numbers and offsets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Path to the file to read."},
                    "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed)."},
                    "limit": {"type": "integer", "description": "Max number of lines to read."}
                },
                "required": ["filePath"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Create or overwrite a file on disk. Can write to temporary scratch space (/tmp/agent_scratch/...), repository files, or user workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Path where the file should be created/written."},
                    "content": {"type": "string", "description": "Full text content to write."}
                },
                "required": ["filePath", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Perform exact search-and-replace in an existing file to modify code or text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Path to the file to edit."},
                    "oldString": {"type": "string", "description": "Exact substring to replace."},
                    "newString": {"type": "string", "description": "New replacement string."}
                },
                "required": ["filePath", "oldString", "newString"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob wildcard pattern (e.g., '*.py', '**/*.js', 'agent_workspace/**/*').",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob wildcard pattern to match."},
                    "path": {"type": "string", "description": "Directory to search from (defaults to '.')."}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for text or regex patterns across files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Search string or regular expression."},
                    "path": {"type": "string", "description": "Directory to search in (defaults to '.')."}
                },
                "required": ["pattern"]
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
                    "query": {"type": "string", "description": "Search query keywords"}
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
                    "media_type": {"type": "string", "enum": ["video", "audio", "auto"], "description": "Type of media. Default is auto."}
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
            "name": "manage_persistent_workspace",
            "description": "Manage files in the user's permanent workspace in the repository under 'agent_workspace/{chat_id}/'. All changes are automatically committed and pushed to GitHub Git so they persist across sessions!",
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

# ──────────────── Compatibility Dummy Schema for OpenCode Zen ────────────────
# OpenCode internal app expects these extra names to be declared in schema
OPENCODE_CLI_DUMMIES = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": "OpenCode CLI compatibility.",
            "parameters": {
                "type": "object",
                "properties": {"dummy": {"type": "string"}}
            }
        }
    }
    for name in ["question", "skill", "task", "todowrite"]
]

ALL_TOOLS = TOOLS + OPENCODE_CLI_DUMMIES

# ──────────────── Layer 1: Persistent Conversation Sessions ────────────────
chat_sessions = {}
chat_sessions_lock = threading.Lock()

def get_session_for_chat(session_key: str) -> str:
    with chat_sessions_lock:
        if session_key not in chat_sessions:
            chat_sessions[session_key] = gen_opencode_session_id()
        return chat_sessions[session_key]

# ──────────────── Layer 2: HTTP/2 Connection Pooling (Bun Client Emulation) ────────────────
http_client = httpx.Client(
    http2=True,
    timeout=httpx.Timeout(120.0, connect=20.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=20, keepalive_expiry=60.0),
)

# ──────────────── Layer 4: Multi-Key Rotation with Cooldowns ────────────────
raw_keys = os.environ.get("OPENCODE_API_KEYS", "").strip() or os.environ.get("OPENCODE_API_KEY", "").strip()
KEY_POOL = [k.strip() for k in raw_keys.split(",") if k.strip()]
if not KEY_POOL:
    KEY_POOL = ["public"]

key_cooldowns = {}
key_index = 0
key_lock = threading.Lock()

def get_next_key(override_key: str = None) -> str:
    global key_index
    if override_key and override_key.strip():
        k = override_key.strip()
        if k not in KEY_POOL:
            KEY_POOL.append(k)
    with key_lock:
        now = time.time()
        for _ in range(len(KEY_POOL)):
            candidate = KEY_POOL[key_index % len(KEY_POOL)]
            key_index += 1
            if key_cooldowns.get(candidate, 0) < now:
                return candidate
        return "public"

def mark_key_cooldown(key: str, cooldown_seconds: float = 180.0):
    with key_lock:
        key_cooldowns[key] = time.time() + cooldown_seconds
        print(f"[OpenCode Key] Put {key[:10]}... on {cooldown_seconds}s cooldown", file=sys.stderr, flush=True)

# ──────────────── Layer 5: Model Fallback Cascading ────────────────
FALLBACK_MODELS = [
    "mimo-v2.6-flash-free",
    "nemotron-3.5-lightning-free",
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "space-bunny-free",
    "beam-free",
    "big-pickle"
]

def call_opencode_stream(messages, requested_model, session_key="default", tools_override=None, initial_key=None):
    pace_request()
    session_id = get_session_for_chat(session_key)

    models_to_try = [requested_model] + [m for m in FALLBACK_MODELS if m != requested_model]
    current_tools = ALL_TOOLS if tools_override is None else tools_override

    for model in models_to_try:
        attempts_for_model = max(2, len(KEY_POOL) + 1)
        for attempt in range(attempts_for_model):
            token = get_next_key(initial_key)
            is_key = (token != "public")
            request_id = gen_opencode_request_id()

            headers = {
                "User-Agent": "opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
                "x-opencode-client": "cli",
                "x-opencode-project": "global",
                "Accept": "*/*",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "x-opencode-session": session_id,
                "x-opencode-request": request_id,
            }

            payload = {
                "model": model,
                "messages": messages,
                "stream": True,
                "temperature": 0.7,
                "max_tokens": 2048
            }
            if current_tools:
                payload["tools"] = current_tools

            url = "https://opencode.ai/zen/v1/chat/completions"

            try:
                with http_client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code == 429:
                        mark_key_cooldown(token, 180.0)
                        print(f"[OpenCode 429] Rate limited on model={model}, key={'key' if is_key else 'public'}. Rotating.", file=sys.stderr, flush=True)
                        time.sleep(1.0)
                        continue
                    elif response.status_code in (401, 403):
                        mark_key_cooldown(token, 600.0)
                        print(f"[OpenCode Auth Error] {response.status_code} for key {token[:10]}...", file=sys.stderr, flush=True)
                        continue
                    elif response.status_code == 400:
                        err_body = response.read().decode('utf-8', errors='ignore')
                        print(f"[OpenCode 400] Model {model} unavailable: {err_body[:150]}. Cascading model.", file=sys.stderr, flush=True)
                        break
                    elif response.status_code != 200:
                        err_body = response.read().decode('utf-8', errors='ignore')
                        print(f"[OpenCode Error] Status {response.status_code}: {err_body[:150]}", file=sys.stderr, flush=True)
                        continue

                    full_content = ""
                    full_reasoning = ""
                    tool_calls_map = {}

                    for line in response.iter_lines():
                        if not line:
                            continue
                        line_str = line.strip()
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

                    if not full_content.strip() and not tool_calls:
                        print(f"[OpenCode Empty Response] Model {model} returned empty answer. Cascading model.", file=sys.stderr, flush=True)
                        break

                    return {
                        "ok": True,
                        "content": full_content,
                        "reasoning": full_reasoning,
                        "tool_calls": tool_calls,
                        "model_used": model
                    }
            except httpx.RequestError as e:
                print(f"[OpenCode Network/HTTP2 Error] {e}", file=sys.stderr, flush=True)
                time.sleep(1.0)
                continue
            except Exception as e:
                print(f"[OpenCode Exception] {e}", file=sys.stderr, flush=True)
                time.sleep(1.0)
                continue

    return {"ok": False, "error": "تمام مدل‌ها و کلیدهای OpenCode با محدودیت یا خطا مواجه شدند. لطفاً لحظاتی دیگر تلاش کنید."}

# ──────────────── Context Manager (200k Context & Compression) ────────────────
def estimate_tokens(messages: list) -> int:
    """Estimates token count across conversation turns (~2 chars per token for code & Persian)."""
    total_chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            total_chars += len(fn.get("name", "")) + len(fn.get("arguments", ""))
    return int(total_chars / 2.0)

def compress_messages_if_needed(messages: list, session_key: str, opencode_key: str) -> list:
    """Compresses conversation history when approaching context limits."""
    tokens = estimate_tokens(messages)
    # Threshold: 140,000 tokens (approx 280,000 chars)
    if tokens < 140000 or len(messages) <= 8:
        return messages

    print(f"[Context Manager] Tokens ({tokens}) exceeded 140k threshold. Compressing conversation history...", flush=True)
    sys_msg = messages[0]
    recent_msgs = messages[-6:]  # Keep last 3 Q&A turns intact
    older_msgs = messages[1:-6]

    older_text = []
    for m in older_msgs:
        role = m.get("role", "")
        content = m.get("content") or ""
        if role in ("user", "assistant") and content:
            older_text.append(f"{role.upper()}: {content[:1000]}")
        elif role == "tool":
            older_text.append(f"TOOL RESULT ({m.get('name')}): {content[:400]}")

    summary_prompt = (
        "You are an expert context compression assistant. Below is an earlier portion of an ongoing conversation.\n"
        "Summarize this conversation into a comprehensive, highly dense Persian summary.\n"
        "Preserve ALL key facts, file names, code snippets, tool execution outcomes, and user requirements.\n\n"
        "CONVERSATION HISTORY:\n" + "\n".join(older_text)
    )

    res = call_opencode_stream(
        [
            {"role": "system", "content": "You summarize conversations concisely, retaining technical details."},
            {"role": "user", "content": summary_prompt}
        ],
        requested_model="mimo-v2.6-flash-free",
        session_key=f"comp_{session_key}",
        tools_override=[],
        initial_key=opencode_key
    )

    summary_content = res.get("content", "").strip() if res.get("ok") else "خلاصه تاریخچه قبلی گفتگو."
    compressed_summary_msg = {
        "role": "system",
        "content": f"📋 **خلاصه فشرده تاریخچه قبلی گفتگو (جهت تداوم کانتکست):**\n{summary_content}"
    }

    compressed_messages = [sys_msg, compressed_summary_msg] + recent_msgs
    new_tokens = estimate_tokens(compressed_messages)
    print(f"[Context Manager] Compressed from {tokens} tokens to {new_tokens} tokens successfully.", flush=True)
    return compressed_messages

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
        "You are 'Mr.3D Super Agent', an advanced autonomous AI software engineer and intelligent assistant running on an Ubuntu runner with full terminal and disk access for Bale messenger.\n\n"
        "WORKSPACES & ARCHITECTURE:\n"
        "1. Ephemeral Scratch Workspace: '/tmp/agent_scratch' or runner local filesystem. Use this for scratch files, running tests, compiling scripts, downloading temporary data, and experimenting.\n"
        "2. Persistent Workspace: 'agent_workspace/{chat_id}/'. Use the 'manage_persistent_workspace' tool to write or modify files here. Anything saved here is committed to Git and pushed to GitHub so it stays permanently available across sessions!\n"
        "3. Repository Root: You can also inspect, read, and edit the repository code and bot files if the user instructs you to work on the bot or repo.\n\n"
        "REAL EXECUTABLE TOOLS:\n"
        "- 'bash': Execute any bash command with stdout/stderr capture (python3, pip, git, curl, ffmpeg, tests, etc.).\n"
        "- 'read': Read any file from disk or repository (supports line ranges).\n"
        "- 'write': Create or overwrite files.\n"
        "- 'edit': Perform exact search-and-replace in existing files.\n"
        "- 'glob': Find files matching pattern.\n"
        "- 'grep': Search for strings or regular expressions across files.\n"
        "- 'web_search': Search the live internet for up-to-date facts, documentation, or news.\n"
        "- 'read_webpage': Extract readable text/markdown from any webpage URL.\n"
        "- 'download_media': Download media from YouTube, Instagram, TikTok, etc. and deliver directly to Bale.\n"
        "- 'generate_image': Generate high quality AI images with Flux and send directly to Bale.\n"
        "- 'take_screenshot': Capture website screenshots and send to Bale.\n"
        "- 'manage_persistent_workspace': Read, write, list, or delete permanent files in repository across sessions.\n"
        "- 'upload_to_gdrive': Upload files to Google Drive and provide a shareable download link.\n\n"
        "OPERATIONAL PRINCIPLES:\n"
        "- Always respond in natural, fluent Persian (Farsi) unless the user requests another language.\n"
        "- Act autonomously: chain tools proactively (e.g. search -> write code -> test with bash -> fix -> save) until the user's objective is fully accomplished.\n"
        "- After tool executions complete, formulate a comprehensive, polite Persian response explaining what was done and the final results."
    )

    messages = [{"role": "system", "content": sys_prompt}]
    if history:
        for turn in history:
            if isinstance(turn, dict) and turn.get("role") and turn.get("content"):
                messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    session_key = f"{chat_id}:{topic_id}"
    MAX_AGENT_TURNS = 10
    final_answer = ""
    tool_results_log = []

    for turn in range(MAX_AGENT_TURNS):
        # Apply 200k context compression check before each call
        messages = compress_messages_if_needed(messages, session_key, opencode_key)

        res = call_opencode_stream(messages, model, session_key=session_key, initial_key=opencode_key)
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
                
                # 1. BASH & SHELL COMMANDS
                if tc_name == "bash":
                    cmd = tc_args.get("command") or ""
                    tm = int(tc_args.get("timeout") or 60)
                    edit_bale_message(chat_id, msg_id, f"💻 **در حال اجرای دستور در محیط لینوکس...**\n`{cmd[:60]}`", bale_token)
                    tool_output = run_bash(cmd, tm)
                    tool_results_log.append(f"اجرای دستور `{cmd[:50]}`:\n{tool_output[:400]}")

                # 2. READ FILE
                elif tc_name == "read":
                    fp = tc_args.get("filePath") or tc_args.get("path") or ""
                    off = int(tc_args.get("offset") or 1)
                    lim = int(tc_args.get("limit") or 300)
                    tool_output = run_read_file(fp, off, lim)
                    tool_results_log.append(f"خواندن فایل '{fp}':\n{tool_output[:300]}")

                # 3. WRITE FILE
                elif tc_name == "write":
                    fp = tc_args.get("filePath") or tc_args.get("path") or ""
                    cnt = tc_args.get("content") or ""
                    edit_bale_message(chat_id, msg_id, f"📝 **در حال ایجاد فایل...**\n`{fp}`", bale_token)
                    tool_output = run_write_file(fp, cnt)
                    tool_results_log.append(f"نوشتن فایل '{fp}': {tool_output}")

                # 4. EDIT FILE
                elif tc_name == "edit":
                    fp = tc_args.get("filePath") or tc_args.get("path") or ""
                    old_str = tc_args.get("oldString") or ""
                    new_str = tc_args.get("newString") or ""
                    edit_bale_message(chat_id, msg_id, f"✏️ **در حال ویرایش فایل...**\n`{fp}`", bale_token)
                    tool_output = run_edit_file(fp, old_str, new_str)
                    tool_results_log.append(f"ویرایش فایل '{fp}': {tool_output}")

                # 5. GLOB
                elif tc_name == "glob":
                    pat = tc_args.get("pattern") or "*"
                    p = tc_args.get("path") or "."
                    tool_output = run_glob_files(pat, p)
                    tool_results_log.append(f"جستجوی فایل‌ها ({pat}):\n{tool_output[:300]}")

                # 6. GREP
                elif tc_name == "grep":
                    pat = tc_args.get("pattern") or ""
                    p = tc_args.get("path") or "."
                    tool_output = run_grep(pat, p)
                    tool_results_log.append(f"جستجوی متن ({pat}):\n{tool_output[:300]}")

                # 7. WEB SEARCH
                elif tc_name in ("web_search", "search"):
                    query = tc_args.get("query") or question
                    edit_bale_message(chat_id, msg_id, f"🔍 **در حال جستجوی اینترنت برای:**\n`{query}`...", bale_token)
                    tool_output = run_web_search(query)
                    tool_results_log.append(f"جستجوی وب برای '{query}':\n{tool_output}")

                # 8. READ WEBPAGE / WEBFETCH
                elif tc_name in ("read_webpage", "webfetch"):
                    url = tc_args.get("url") or ""
                    edit_bale_message(chat_id, msg_id, f"📄 **در حال خواندن صفحه وب:**\n`{url}`...", bale_token)
                    tool_output = run_read_webpage(url)
                    tool_results_log.append(f"صفحه '{url}':\n{tool_output[:400]}")

                # 9. DOWNLOAD MEDIA
                elif tc_name == "download_media":
                    url = tc_args.get("url") or ""
                    m_type = tc_args.get("media_type") or "auto"
                    tool_output = run_download_media(url, m_type, chat_id, bale_token, msg_id)
                    tool_results_log.append(f"دانلود مدیا: {tool_output}")

                # 10. GENERATE IMAGE
                elif tc_name == "generate_image":
                    prompt = tc_args.get("prompt") or question
                    tool_output = run_generate_image(prompt, chat_id, bale_token, msg_id)
                    tool_results_log.append(f"تولید تصویر: {tool_output}")

                # 11. TAKE SCREENSHOT
                elif tc_name == "take_screenshot":
                    url = tc_args.get("url") or ""
                    tool_output = run_take_screenshot(url, chat_id, bale_token, msg_id)
                    tool_results_log.append(f"اسکرین‌شات: {tool_output}")

                # 12. PERSISTENT WORKSPACE
                elif tc_name in ("manage_persistent_workspace", "manage_workspace"):
                    action = tc_args.get("action") or "list"
                    fn = tc_args.get("filename") or ""
                    cnt = tc_args.get("content") or ""
                    edit_bale_message(chat_id, msg_id, f"💾 **مدیریت فضای کاری در ریپازیتوری ({action})...**", bale_token)
                    tool_output = run_manage_workspace(action, fn, cnt, chat_id)
                    tool_results_log.append(f"فضای کاری ({action}): {tool_output}")

                # 13. GOOGLE DRIVE UPLOAD
                elif tc_name == "upload_to_gdrive":
                    fp = tc_args.get("file_path") or ""
                    fid = tc_args.get("drive_folder_id") or ""
                    tool_output = run_upload_to_gdrive(fp, fid, chat_id)
                    tool_results_log.append(f"گوگل درایو: {tool_output}")

                else:
                    tool_output = "عملیات با موفقیت انجام شد."

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tc_name or "unknown",
                    "content": tool_output
                })

            if turn == MAX_AGENT_TURNS - 2:
                edit_bale_message(chat_id, msg_id, "✍️ **در حال جمع‌بندی نهایی و نگارش پاسخ...**", bale_token)
                messages.append({
                    "role": "user",
                    "content": "با توجه به تمام ابزارهای اجرا شده و نتایج آن‌ها، پاسخ نهایی و کامل را به زبان فارسی ارائه بده."
                })
        else:
            final_answer = content.strip() or reasoning.strip()
            break

    # If final answer is empty but tools executed, synthesize
    if not final_answer and tool_results_log:
        print("[Agent Fallback] Synthesizing answer from tool results...", flush=True)
        edit_bale_message(chat_id, msg_id, "✍️ **در حال جمع‌بندی نهایی...**", bale_token)
        synth_msgs = [
            {"role": "system", "content": "You are a helpful Persian AI assistant named Mr.3D Super Agent. Answer the user comprehensively in Persian based on the actions taken and results."},
            {"role": "user", "content": f"درخواست کاربر: {question}\n\nاقدامات انجام شده:\n" + "\n\n".join(tool_results_log[:8]) + "\n\nپاسخ نهایی، مستند و شیوا را به زبان فارسی بنویس."}
        ]
        synth_res = call_opencode_stream(synth_msgs, "nemotron-3.5-lightning-free", session_key=session_key, tools_override=[], initial_key=opencode_key)
        if synth_res.get("ok"):
            final_answer = synth_res.get("content", "").strip() or synth_res.get("reasoning", "").strip()

    # Emergency fallback
    if not final_answer:
        em_res = call_opencode_stream([
            {"role": "system", "content": "You are a helpful Persian AI assistant."},
            {"role": "user", "content": question}
        ], "nemotron-3.5-lightning-free", session_key=session_key, tools_override=[], initial_key=opencode_key)
        if em_res.get("ok"):
            final_answer = em_res.get("content", "").strip()

    if not final_answer:
        edit_bale_message(chat_id, msg_id, "❌ متأسفانه دریافت پاسخ از سرور هوش مصنوعی با خطا مواجه شد. لطفاً دوباره پیام خود را بفرستید.", bale_token)
        return

    # Strip leaked reasoning headers if any
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
    ensure_git_config()
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
