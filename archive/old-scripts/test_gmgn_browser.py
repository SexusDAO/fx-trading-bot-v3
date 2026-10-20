#!/usr/bin/env python3
"""
方案1：用 Playwright 绕过 Cloudflare，提取 WebSocket credentials
"""

import asyncio
import json
from playwright.async_api import async_playwright

async def extract_gmgn_credentials():
    """用浏览器打开 GMGN，提取 cookies 和 tokens"""
    
    print("=" * 60)
    print("方案1：浏览器自动化绕过 Cloudflare")
    print("=" * 60)
    
    async with async_playwright() as p:
        # 启动浏览器（无头模式）
        print("\n[1/5] 启动 Chromium 浏览器...")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        # 访问 GMGN
        print("[2/5] 访问 https://gmgn.ai ...")
        try:
            await page.goto("https://gmgn.ai", timeout=60000)
            print("✅ 页面加载成功")
        except Exception as e:
            print(f"⚠️ 页面加载超时或出错: {e}")
        
        # 等待 Cloudflare challenge
        print("[3/5] 等待 Cloudflare challenge 通过...")
        print("      （这可能需要 10-30 秒）")
        
        # 等待页面完全加载（Cloudflare 通过后会有实际内容）
        try:
            await page.wait_for_load_state("networkidle", timeout=60000)
            print("✅ 页面加载完成")
        except:
            print("⚠️ 等待超时，继续尝试...")
        
        # 额外等待，确保 JavaScript 执行完成
        await asyncio.sleep(5)
        
        # 提取 cookies
        print("[4/5] 提取浏览器 cookies...")
        cookies = await context.cookies()
        print(f"✅ 获取到 {len(cookies)} 个 cookies")
        
        # 提取 localStorage
        print("[5/5] 提取 localStorage...")
        localStorage = await page.evaluate("""
            () => {
                const data = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    data[key] = localStorage.getItem(key);
                }
                return data;
            }
        """)
        print(f"✅ 获取到 {len(localStorage)} 个 localStorage 项")
        
        # 保存结果
        result = {
            "cookies": cookies,
            "localStorage": localStorage,
            "url": page.url,
            "title": await page.title()
        }
        
        with open("/root/.openclaw/workspace/gmgn_credentials.json", "w") as f:
            json.dump(result, f, indent=2)
        
        print("\n" + "=" * 60)
        print("✅ Credentials 已保存到 gmgn_credentials.json")
        print("=" * 60)
        
        # 显示关键信息
        print("\n📋 Cookies 列表:")
        for cookie in cookies:
            print(f"  - {cookie['name']}: {cookie['value'][:50]}...")
        
        print("\n📋 localStorage 键:")
        for key in localStorage.keys():
            print(f"  - {key}")
        
        # 尝试找到 JWT token
        print("\n🔍 查找 JWT token...")
        jwt_found = False
        for key, value in localStorage.items():
            if "token" in key.lower() or "jwt" in key.lower() or "auth" in key.lower():
                print(f"  ✅ {key}: {value[:100]}...")
                jwt_found = True
        
        # 检查 cookies 中是否有 token
        for cookie in cookies:
            if "token" in cookie['name'].lower() or "jwt" in cookie['name'].lower():
                print(f"  ✅ Cookie {cookie['name']}: {cookie['value'][:100]}...")
                jwt_found = True
        
        if not jwt_found:
            print("  ⚠️ 未找到明显的 JWT token")
            print("  💡 可能需要登录 GMGN 账号")
        
        await browser.close()
        
        return result

if __name__ == "__main__":
    asyncio.run(extract_gmgn_credentials())
