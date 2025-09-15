import asyncio
import platform

# Fix for Playwright asyncio subprocess error on Windows
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import streamlit as st
import aiohttp
from playwright.async_api import async_playwright
from langgraph.graph import StateGraph
import httpx
import os

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def sanitize_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url

async def gemini_suggest_selectors(html):
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
    prompt = {
        "contents": [{
            "parts": [{
                "text": (
                    "Given this login page HTML, suggest probable CSS selectors for username, password, and submit button."
                )
            }, {
                "text": html
            }]
        }]
    }
    headers = {"Authorization": f"Bearer {GEMINI_API_KEY}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint, json=prompt, headers=headers) as response:
            content = await response.json()
            for part in content.get("candidates", []):
                selectors = part.get("content", {}).get("parts", [])
                for selector in selectors:
                    if "username" in selector or "password" in selector or "submit" in selector:
                        return selector
    return None

class ValidateNode:
    async def run(self, context):
        url = context["url"]
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=5)
                context["html"] = resp.text
                context["status"] = "validated"
        except Exception as e:
            context["error"] = str(e)
            context["status"] = "failed"
        return context

class FindSelectorsNode:
    async def run(self, context):
        html = context["html"]
        # Updated: Use fixed selectors for your attached login page
        context["selectors"] = {
            "username": "#username",
            "password": "#password",
            "submit": "button[type='submit']"
        }
        context["status"] = "selectors_found"
        return context

class LoginNode:
    async def run(self, context):
        url = context["url"]
        username = context["username"]
        password = context["password"]
        selectors = context.get("selectors", {})
        logs = []
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                clean_url = sanitize_url(url)
                await page.goto(clean_url)
                logs.append(f"Visited {clean_url}")

                await page.wait_for_selector(selectors['username'])
                await page.fill(selectors['username'], username)
                logs.append("Filled username")

                await page.wait_for_selector(selectors['password'])
                await page.fill(selectors['password'], password)
                logs.append("Filled password")

                await page.wait_for_selector(selectors['submit'])
                await page.click(selectors['submit'])
                logs.append("Clicked submit")

                await page.wait_for_load_state('networkidle', timeout=7000)

                screenshot_path = "screenshot.png"
                await page.screenshot(path=screenshot_path)
                context["final_url"] = page.url
                context["screenshot"] = screenshot_path
                context["logs"] = logs
                await browser.close()
                context["status"] = "login_attempted"
        except Exception as e:
            logs.append(f"Exception: {str(e)}")
            context["error"] = str(e)
            context["logs"] = logs
            context["status"] = "login_failed"
        return context

def build_graph():
    graph = StateGraph(dict)

    graph.add_node("validate", ValidateNode().run)
    graph.add_node("find_selectors", FindSelectorsNode().run)
    graph.add_node("login", LoginNode().run)

    graph.add_node("fail", lambda ctx: ctx)

    graph.add_conditional_edges(
        "validate",
        lambda ctx: "find_selectors" if ctx.get("status") == "validated" else "fail",
        {"find_selectors": "find_selectors", "fail": "fail"}
    )
    graph.add_conditional_edges(
        "find_selectors",
        lambda ctx: "login" if ctx.get("status") == "selectors_found" else "fail",
        {"login": "login", "fail": "fail"}
    )

    graph.set_entry_point("validate")
    compiled = graph.compile()
    return compiled

async def run_login_flow(context):
    graph = build_graph()
    returned_context = await graph.ainvoke(context)
    return returned_context

st.title("Async Automated Login Tester (AI-powered)")

url = st.text_input("Login URL")
if url and not url.startswith(("http://", "https://")):
    st.warning("Input URL missing protocol, automatically adding https://")
    url = "https://" + url

username = st.text_input("Username")
password = st.text_input("Password", type="password")
start_btn = st.button("Test Login")

if start_btn:
    result = asyncio.run(run_login_flow({"url": url, "username": username, "password": password}))

    st.write("## Final URL")
    st.write(result.get("final_url", "Login failed"))

    st.write("## Logs")
    for log in result.get("logs", []):
        st.write(log)

    st.write("## Screenshot")
    if result.get("screenshot"):
        st.image("screenshot.png")

    if "error" in result:
        st.error(result["error"])

