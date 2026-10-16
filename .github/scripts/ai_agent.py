#!/usr/bin/env python3
import os
import sys
import json
import time
import secrets
import threading
import urllib.request
import urllib.parse
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

# ──────────────── Web Search Tool ────────────────
def run_web_search(query: str) -> str:
    print(f"[Agent] Executing web search: {query}", flush=True)
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
            
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=4))
            formatted = []
            for r in raw_results:
                title = r.get('title', '').strip()
                body = r.get('body', '').strip()
                href = r.get('href', '').strip()
                if title or body:
                    formatted.append(f"- {title}: {body} ({href})")
            return "\n".join(formatted) if formatted else "نتیجه‌ای یافت نشد."
    except Exception as e:
        print(f"[Search Error] {e}", file=sys.stderr, flush=True)
        return f"خطا در جستجو: {e}"

TOOLS = [
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
    }
]

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
            except Exception:
                pass
            time.sleep(attempt + 1)
        except Exception as e:
            time.sleep(attempt + 1)
    return None

def edit_bale_message(chat_id, message_id, text, token):
    if not message_id:
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

# ──────────────── OpenCode SSE Caller ────────────────
def call_opencode_stream(messages, model, api_key):
    pace_request()
    
    auth_tokens_to_try = []
    if api_key and api_key.strip():
        auth_tokens_to_try.append(api_key.strip())
    auth_tokens_to_try.append("public")
    
    url = "https://opencode.ai/zen/v1/chat/completions"
    
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "tools": TOOLS,
        "temperature": 0.7,
        "max_tokens": 2048
    }
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    
    delays = [3, 6, 15]
    
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
                resp = urllib.request.urlopen(req, timeout=60)
                full_content = ""
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
                        delta = chunk.get('choices', [{}])[0].get('delta', {})
                        if 'content' in delta and delta['content']:
                            full_content += delta['content']
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
                
                tool_calls = list(tool_calls_map.values()) if tool_calls_map else None
                return {
                    "ok": True,
                    "content": full_content,
                    "tool_calls": tool_calls
                }
            except urllib.error.HTTPError as e:
                status = e.code
                err_body = e.read().decode('utf-8', errors='ignore')
                print(f"[OpenCode Error] Status {status} (token={'key' if is_key else 'public'}): {err_body[:200]}", file=sys.stderr, flush=True)
                if status in (401, 403):
                    # Break out to try the next auth candidate (e.g. public)
                    break
                elif status == 429:
                    # Rate limit; back off using intervals [3, 6, 15]
                    continue
                else:
                    if attempt < len(delays):
                        continue
            except Exception as e:
                print(f"[OpenCode Network Error] {e}", file=sys.stderr, flush=True)
                if attempt < len(delays):
                    continue
                    
    return {"ok": False, "error": "OpenCode API request failed on all attempts."}

# ──────────────── Main Agent Runner ────────────────
def main():
    bale_token = os.environ.get("BALE_BOT_TOKEN", "").strip()
    opencode_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    question = os.environ.get("QUESTION", "").strip()
    model = os.environ.get("MODEL", "").strip() or "mimo-v2.6-flash-free"
    chat_id = os.environ.get("CHAT_ID", "").strip()
    msg_id = os.environ.get("MSG_ID", "").strip()
    group_chat_id = os.environ.get("GROUP_CHAT_ID", "").strip()
    worker_url = os.environ.get("WORKER_URL", "").strip()
    
    history_json_str = os.environ.get("HISTORY_JSON", "").strip()
    history = []
    if history_json_str:
        try:
            parsed = json.loads(history_json_str)
            if isinstance(parsed, list):
                history = parsed
        except Exception as e:
            print(f"[History Parse Warning] {e}", file=sys.stderr)

    if not question:
        print("[Error] No question provided", file=sys.stderr)
        if chat_id:
            edit_bale_message(chat_id, msg_id, "❌ سوالی دریافت نشد.", bale_token)
        sys.exit(0)

    print(f"[Agent Started] Chat: {chat_id}, Model: {model}, Question: {question[:80]}", flush=True)

    sys_prompt = (
        "You are an advanced, intelligent Persian AI Assistant named 'Mr.3D AI'.\n"
        "You have access to a live 'web_search' tool to browse the real-time internet, search for current facts, news, and technical information.\n"
        "Always respond in fluent, natural Persian (Farsi) unless the user specifically asks in another language.\n"
        "When you use web_search, formulate clear, concise queries and synthesize the findings naturally into your answer without showing raw search syntax."
    )

    messages = [{"role": "system", "content": sys_prompt}]
    
    # Inject context window history
    if history:
        for turn in history:
            if isinstance(turn, dict) and turn.get("role") and turn.get("content"):
                messages.append({"role": turn["role"], "content": turn["content"]})
                
    messages.append({"role": "user", "content": question})

    # Step 1: Call Model
    res1 = call_opencode_stream(messages, model, opencode_key)
    if not res1.get("ok"):
        err_msg = res1.get("error", "خطا در برقراری ارتباط با مدل هوش مصنوعی.")
        edit_bale_message(chat_id, msg_id, f"❌ {err_msg}", bale_token)
        sys.exit(0)

    tool_calls = res1.get("tool_calls")
    
    # Step 2: Check for Tool Calls (Agentic Web Search Loop)
    if tool_calls:
        print(f"[Agent Tool Call] Model requested {len(tool_calls)} tool call(s)", flush=True)
        asst_msg = {
            "role": "assistant",
            "content": res1.get("content") or None,
            "tool_calls": [
                {
                    "id": tc["id"] or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                }
                for i, tc in enumerate(tool_calls)
            ]
        }
        messages.append(asst_msg)

        for i, tc in enumerate(tool_calls):
            tc_name = tc.get("name")
            tc_args_str = tc.get("arguments", "{}")
            tc_id = tc.get("id") or f"call_{i}"

            if tc_name == "web_search":
                query = question
                try:
                    args = json.loads(tc_args_str)
                    query = args.get("query") or question
                except Exception:
                    pass

                # Live status update in Bale chat
                edit_bale_message(chat_id, msg_id, f"🔍 **در حال جستجوی وب برای:**\n`{query}`...", bale_token)
                
                search_res = run_web_search(query)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": "web_search",
                    "content": search_res
                })

        # Step 3: Second call to generate grounded answer
        edit_bale_message(chat_id, msg_id, "✍️ **در حال جمع‌بندی و نگارش پاسخ با داده‌های زنده وب...**", bale_token)
        res2 = call_opencode_stream(messages, model, opencode_key)
        if not res2.get("ok"):
            err_msg = res2.get("error", "خطا در دریافت پاسخ نهایی از مدل.")
            edit_bale_message(chat_id, msg_id, f"❌ {err_msg}", bale_token)
            sys.exit(0)
            
        final_answer = res2.get("content", "").strip()
    else:
        final_answer = res1.get("content", "").strip()

    if not final_answer:
        edit_bale_message(chat_id, msg_id, "❌ پاسخ خالی از هوش مصنوعی دریافت شد. لطفاً دوباره تلاش کنید.", bale_token)
        sys.exit(0)

    # Defensive formatting: remove leaked reasoning headers if any
    hdr = final_answer.rfind('**Answer**')
    if hdr != -1 and re.search(r'\*\*Reasoning|Reasoning and information|Reasoning used', final_answer[:hdr], re.IGNORECASE):
        final_answer = final_answer[hdr + len('**Answer**'):].strip()

    full_text = f"🤖 **پاسخ هوش مصنوعی** ({model}):\n\n{final_answer}"
    
    # Deliver answer to user
    edit_bale_message(chat_id, msg_id, full_text, bale_token)

    # Optional group chat notification
    if group_chat_id and group_chat_id != "0":
        send_bale_message(group_chat_id, f"🤖 **AI Answer**\n\n{final_answer}", bale_token)

    # Update conversation history on Cloudflare Worker
    if worker_url and worker_url.startswith("http"):
        try:
            update_url = f"{worker_url.rstrip('/')}/ai-history-update"
            update_body = json.dumps({
                "chat_id": chat_id,
                "user_message": question,
                "assistant_message": final_answer
            }, ensure_ascii=False).encode('utf-8')
            ureq = urllib.request.Request(update_url, data=update_body, headers={"Content-Type": "application/json", "User-Agent": "CFWorker-Agent/1.0"}, method="POST")
            with urllib.request.urlopen(ureq, timeout=10) as uresp:
                print(f"[History Updated] Status: {uresp.status}", flush=True)
        except Exception as e:
            print(f"[History Update Note] Could not update worker history: {e}", file=sys.stderr, flush=True)

    print("[Agent Finished Successfully]", flush=True)

if __name__ == "__main__":
    main()
