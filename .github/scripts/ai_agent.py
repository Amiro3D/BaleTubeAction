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
            return "\n".join(formatted) if formatted else "نتیجه‌ای در جستجو یافت نشد."
    except Exception as e:
        print(f"[Search Error] {e}", file=sys.stderr, flush=True)
        return f"خطا در جستجو: {e}"

def run_read_file(filepath: str) -> str:
    print(f"[Agent] Reading file: {filepath}", flush=True)
    try:
        clean_path = os.path.basename(filepath)
        if os.path.exists(clean_path):
            with open(clean_path, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()[:2000]
        return f"File '{filepath}' not found or cannot be accessed."
    except Exception as e:
        return f"Error reading file: {e}"

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
                if e.code in (400, 401, 403, 404):
                    return j
            except Exception:
                pass
            if e.code in (400, 401, 403, 404):
                return None
            time.sleep(attempt + 1)
        except Exception as e:
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
    history = job_data.get("history") or []

    if not question:
        return

    print(f"\n[Agent Processing] Chat: {chat_id}, Model: {model}, Question: {question[:80]}", flush=True)

    sys_prompt = (
        "You are an advanced, intelligent Persian AI Assistant named 'Mr.3D AI'.\n"
        "You have access to a live 'web_search' tool to browse the real-time internet, search for current facts, news, and technical information.\n"
        "Always respond in fluent, natural Persian (Farsi) unless the user specifically asks in another language.\n"
        "When you use web_search, formulate clear queries and synthesize the findings naturally into your answer."
    )

    messages = [{"role": "system", "content": sys_prompt}]
    if history:
        for turn in history:
            if isinstance(turn, dict) and turn.get("role") and turn.get("content"):
                messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    MAX_AGENT_TURNS = 3
    final_answer = ""
    tool_results_log = []

    for turn in range(MAX_AGENT_TURNS):
        res = call_opencode_stream(messages, model, opencode_key)
        if not res.get("ok"):
            err_msg = res.get("error", "خطا در برقراری ارتباط با مدل هوش مصنوعی.")
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

                if tc_name == "web_search":
                    query = question
                    try:
                        args = json.loads(tc_args_str)
                        query = args.get("query") or question
                    except Exception:
                        pass

                    edit_bale_message(chat_id, msg_id, f"🔍 **در حال جستجوی وب برای:**\n`{query}`...", bale_token)
                    search_res = run_web_search(query)
                    tool_results_log.append(f"جستجو برای '{query}':\n{search_res}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": "web_search",
                        "content": search_res
                    })
                elif tc_name == "read":
                    filepath = ""
                    try:
                        args = json.loads(tc_args_str)
                        filepath = args.get("filePath") or args.get("path") or ""
                    except Exception:
                        pass
                    read_res = run_read_file(filepath)
                    tool_results_log.append(f"خواندن فایل '{filepath}':\n{read_res}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": "read",
                        "content": read_res
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": tc_name or "unknown",
                        "content": "Tool executed."
                    })

            if turn == MAX_AGENT_TURNS - 2:
                edit_bale_message(chat_id, msg_id, "✍️ **در حال جمع‌بندی و نگارش پاسخ با داده‌های زنده وب...**", bale_token)
                messages.append({
                    "role": "user",
                    "content": "با توجه به تمام اطلاعات و نتایج جستجوی بالا، لطفاً اکنون پاسخ نهایی، جامع و کامل را به زبان فارسی بنویس."
                })
        else:
            final_answer = content.strip() or reasoning.strip()
            break

    # If final answer is still empty after loop but tools were run, synthesize from results
    if not final_answer and tool_results_log:
        print("[Agent Fallback] Synthesizing answer from search results...", flush=True)
        edit_bale_message(chat_id, msg_id, "✍️ **در حال نگارش پاسخ با اطلاعات به دست آمده...**", bale_token)
        synth_msgs = [
            {"role": "system", "content": "You are a helpful Persian AI assistant named Mr.3D AI. Answer the user question comprehensively in Persian based on the search context."},
            {"role": "user", "content": f"سوال کاربر: {question}\n\nاطلاعات جستجوشده از وب:\n" + "\n\n".join(tool_results_log[:5]) + "\n\nلطفاً پاسخ کامل، مفید و ساختاریافته به زبان فارسی ارائه بده."}
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
        edit_bale_message(chat_id, msg_id, "❌ متأسفانه دریافت پاسخ از سرور هوش مصنوعی با خطا مواجه شد. لطفاً دوباره سوال خود را ارسال فرمایید.", bale_token)
        return

    # Clean leaked reasoning headers if any
    hdr = final_answer.rfind('**Answer**')
    if hdr != -1 and re.search(r'\*\*Reasoning|Reasoning and information|Reasoning used', final_answer[:hdr], re.IGNORECASE):
        final_answer = final_answer[hdr + len('**Answer**'):].strip()

    full_text = f"🤖 **پاسخ هوش مصنوعی** ({model}):\n\n{final_answer}"

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

    print(f"[Job Finished Successfully for Chat {chat_id}]", flush=True)

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
    while time.time() - start_time < MAX_LIFESPAN:
        try:
            poll_url = f"{worker_url.rstrip('/')}/ai-poll"
            req = urllib.request.Request(poll_url, headers={"User-Agent": "GitHubActions-AIRunner/1.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                poll_fail_count = 0
                job = data.get("job")
                if job and isinstance(job, dict) and job.get("question"):
                    process_job(job, bale_token, opencode_key, worker_url)
                    continue
        except Exception as e:
            poll_fail_count += 1
            if poll_fail_count % 30 == 0:
                print(f"[Poll Notice] Repeated poll network error: {e}", file=sys.stderr, flush=True)
            time.sleep(2.0)
            continue

        time.sleep(1.2)

    print("[Runner Daemon Finished] Lifespan limit reached. Exiting cleanly to allow fresh runner next time.", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL AGENT ERROR] {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
