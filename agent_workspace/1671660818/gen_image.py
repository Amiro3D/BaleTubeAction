#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ساخت تصویر با مدل‌های رایگان Pollinations.ai
بدون نیاز به API Key - فقط کافیه یک URL ساده بگیریم
"""
import urllib.request
import urllib.parse
import sys
import time

def generate_image(prompt: str, output_path: str, model: str = "flux",
                   width: int = 1280, height: int = 800, seed: int = None):
    """
    ساخت تصویر با Pollinations.ai
    مدل‌های رایگان: flux (سریع/خوب), turbo (خیلی سریع)
    """
    encoded_prompt = urllib.parse.quote(prompt)
    url = (f"https://image.pollinations.ai/prompt/{encoded_prompt}"
           f"?model={model}&width={width}&height={height}&nologo=true")
    if seed is not None:
        url += f"&seed={seed}"
    
    print(f"[+] Model: {model}")
    print(f"[+] Prompt: {prompt}")
    print(f"[+] URL: {url[:100]}...")
    print(f"[+] در حال ساخت تصویر (تا ۶۰ ثانیه)...", flush=True)
    
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    start = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    elapsed = time.time() - start
    
    if len(data) < 1000:
        print("[-] خطا: تصویر دریافتی نامعتبر است", file=sys.stderr)
        sys.exit(1)
    
    with open(output_path, "wb") as f:
        f.write(data)
    
    print(f"[+] تصویر ذخیره شد: {output_path}")
    print(f"[+] حجم: {len(data)/1024:.1f} KB")
    print(f"[+] زمان: {elapsed:.1f} ثانیه")
    return output_path

if __name__ == "__main__":
    prompt = ("Majestic futuristic cyberpunk city at night, glowing neon signs in "
              "Persian and English, flying cars, giant holographic cat sitting on a "
              "skyscraper, rain reflections on the streets, cinematic lighting, "
              "ultra detailed, 8k, studio quality")
    generate_image(prompt, "/tmp/agent_scratch/cyberpunk_city.png",
                   model="flux", width=1280, height=800, seed=42)
