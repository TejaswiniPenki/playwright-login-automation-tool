import asyncio
import platform
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import streamlit as st
import aiohttp
from playwright.async_api import async_playwright
from playwright_stealth import Stealth  # stealth import
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
                "text": "Given this login page HTML, suggest probable CSS selectors for username, password, and submit button."
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
        url = context.get("url", "")
        if "accounts.google.com" in url:
            context["selectors"] = {}
        else:
            context["selectors"] = {
                "username": "#username",
                "password": "#password",
                "submit": "button[type='submit'], input[type='submit']"
            }
        context["status"] = "selectors_found"
        return context

class LoginNode:
    async def run(self, context):
        import asyncio  # ensure asyncio is imported here for sleep
        url = context["url"]
        user_input = context["user_input"]
        password = context["password"]
        selectors = context.get("selectors", {})
        logs = []
        stealth = Stealth()
        try:
            async with stealth.use_async(async_playwright()) as p:
                browser = await p.chromium.launch(
                    headless=False,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--disable-web-security',
                        '--disable-features=IsolateOrigins,site-per-process'
                    ]
                )
                context_browser = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"
                    ),
                    timezone_id="America/New_York",
                    locale="en-US"
                )
                # Increase default timeouts
                context_browser.set_default_navigation_timeout(150000)  # 2.5 minutes
                context_browser.set_default_timeout(150000)

                page = await context_browser.new_page()
                page.set_default_timeout(150000)

                clean_url = sanitize_url(url)
                await page.goto(clean_url)
                logs.append(f"Visited {clean_url}")

                # Log console and other page errors to logs
                page.on("console", lambda msg: logs.append(f"Console: {msg.text}"))
                page.on("pageerror", lambda exc: logs.append(f"Page error: {exc.message}"))

                if "accounts.google.com" in clean_url:
                    await page.wait_for_selector('input[type="email"]', timeout=150000)
                    await page.fill('input[type="email"]', user_input)
                    await page.click('#identifierNext')
                    logs.append("Filled email or username and clicked Next (Gmail step 1)")
                    await page.wait_for_timeout(2000)
                    await page.locator('input[type="password"]').wait_for(state='attached', timeout=150000)
                    password_input = page.locator('input[type="password"]')
                    await password_input.fill(password, timeout=150000)
                    await page.click('#passwordNext')
                    logs.append("Filled password and clicked Next (Gmail step 2)")
                    await page.wait_for_load_state('networkidle', timeout=150000)
                    logs.append("Gmail login completed, network idle detected")

                    if "mail.google.com" in page.url:
                        logs.append("Detected Gmail inbox page, ending login flow")
                        context["final_url"] = page.url
                        context["logs"] = logs

                        screenshot_path = "screenshot.png"
                        await page.screenshot(path=screenshot_path)
                        context["screenshot"] = screenshot_path

                        html_content = await page.content()
                        context["html_content"] = html_content

                        await browser.close()
                        context["status"] = "login_attempted"
                        return context

                if selectors.get("username"):
                    await page.wait_for_selector(selectors["username"], timeout=150000)
                    await page.fill(selectors["username"], user_input)
                    logs.append("Filled username or email")

                    await page.wait_for_selector(selectors["password"], timeout=150000)
                    await page.fill(selectors["password"], password)
                    logs.append("Filled password")

                    await page.wait_for_selector(selectors["submit"], timeout=150000)

                    old_url = page.url
                    await page.click(selectors["submit"])
                    logs.append("Clicked submit button")

                    # Poll URL change or wait for the Salesforce main app URL pattern
                    for _ in range(60):
                        await asyncio.sleep(2)
                        current_url = page.url
                        if current_url != old_url:
                            logs.append(f"URL changed after login submit to {current_url}")
                            # Wait for known Salesforce app URL pattern or UI element
                            if "my.salesforce.com/one/one.app" in current_url:
                                logs.append("Detected Salesforce main app URL pattern")
                                break
                        else:
                            logs.append("URL not changed yet, continuing to poll")

                    else:
                        logs.append("URL did not change after login submit within timeout")

                    # Wait for main Salesforce UI selector instead of networkidle
                    try:
                        await page.wait_for_selector("div.oneAppNavBar", timeout=90000)
                        logs.append("Found main Salesforce app UI element")
                    except Exception:
                        logs.append("Did not find main Salesforce app UI element within timeout")

                    await page.wait_for_timeout(8000)  # Allow UI stabilize
                    logs.append("Waited additional 8s for UI stabilization")

                screenshot_path = "screenshot.png"
                await page.screenshot(path=screenshot_path)
                html_content = await page.content()

                context["final_url"] = page.url
                context["screenshot"] = screenshot_path
                context["html_content"] = html_content
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
        {"find_selectors": "find_selectors", "fail": "fail"},
    )
    graph.add_conditional_edges(
        "find_selectors",
        lambda ctx: "login" if ctx.get("status") == "selectors_found" else "fail",
        {"login": "login", "fail": "fail"},
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
user_input = st.text_input("Username or Email")
password = st.text_input("Password", type="password")
start_btn = st.button("Test Login")
if start_btn:
    input_context = {
        "url": url,
        "user_input": user_input,
        "password": password,
    }
    result = asyncio.run(run_login_flow(input_context))

    # Write html content to file after async run finishes
    if "html_content" in result:
        try:
            with open("page_content.html", "w", encoding="utf-8") as f:
                f.write(result["html_content"])
            result["html_file"] = "page_content.html"
        except Exception as e:
            st.error(f"Error writing HTML file: {e}")

    st.write("## Final URL")
    st.write(result.get("final_url", "Login failed"))
    st.write("## Logs")
    for log in result.get("logs", []):
        st.write(log)
    st.write("## Screenshot")
    if result.get("screenshot"):
        st.image(result["screenshot"])
    st.write("## Page HTML")
    if result.get("html_file"):
        try:
            with open(result["html_file"], "r", encoding="utf-8") as f:
                st.code(f.read()[:1000] + "\n...")
        except Exception as e:
            st.error(f"Error reading HTML file: {e}")
    if "error" in result:
        st.error(result["error"])
