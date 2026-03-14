"""
浏览器全流程支付 - 用 Playwright 在真实浏览器中执行 Stripe.js

核心思路: 不走 curl_cffi API 模拟, 而是在 Playwright 浏览器中加载 Stripe.js,
让 Stripe.js 自然执行设备指纹采集 (m.stripe.com/6 iframe + canvas + WebGL 等),
然后用真实浏览器环境完成 payment_method 创建 + confirm + hCaptcha 处理。

真实 Stripe.js 环境的优势:
  1. m.stripe.com/6 指纹采集自然发生, 产生高质量 guid
  2. hCaptcha 如果触发, 浏览器环境评分更高
  3. 所有 TLS/JS 指纹完全真实
  4. stripe.js handleNextAction() 自动处理 hCaptcha
"""
import json
import logging
import os
import random
import re
import subprocess
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


class BrowserPayment:
    """用真实浏览器 + Stripe.js 执行完整支付流程"""

    def __init__(
        self,
        proxy: str = None,
        headless: bool = False,
        slow_mo: int = 50,
    ):
        self.proxy = proxy
        self.headless = headless
        self.slow_mo = slow_mo

    def create_checkout_session(self, session_token: str, access_token: str,
                                device_id: str, chatgpt_proxy: str = None,
                                billing_country: str = "GB",
                                billing_currency: str = "GBP") -> dict:
        """
        用 ChatGPT API 创建 checkout session, 返回 checkout 数据。
        这一步必须走 ChatGPT API (需要认证)。
        """
        from http_client import create_http_session

        session = create_http_session(proxy=chatgpt_proxy)
        session.cookies.set("__Secure-next-auth.session-token", session_token, domain=".chatgpt.com")
        if device_id:
            session.cookies.set("oai-did", device_id, domain=".chatgpt.com")

        # sentinel token
        did = device_id or str(uuid.uuid4())
        sentinel_body = json.dumps({"p": "", "id": did, "flow": "authorize_continue"})
        sentinel_headers = {
            "Origin": "https://sentinel.openai.com",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
            "Content-Type": "text/plain;charset=UTF-8",
        }
        resp = session.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            headers=sentinel_headers,
            data=sentinel_body,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Sentinel Token 获取失败: {resp.status_code}")
        sentinel_token = json.dumps({
            "p": "", "t": "", "c": resp.json().get("token", ""),
            "id": did, "flow": "authorize_continue"
        })

        # checkout
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "oai-device-id": did,
            "openai-sentinel-token": sentinel_token,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        body = {
            "plan_name": "chatgptteamplan",
            "team_plan_data": {
                "workspace_name": "Artizancloud",
                "price_interval": "month",
                "seat_quantity": 5,
            },
            "billing_details": {
                "country": billing_country,
                "currency": billing_currency,
            },
            "cancel_url": "https://chatgpt.com/?promo_campaign=team0dollar#team-pricing",
            "promo_campaign": {
                "promo_campaign_id": "team0dollar",
                "is_coupon_from_query_param": True,
            },
            "checkout_ui_mode": "custom",
        }
        resp = session.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            headers=headers,
            json=body,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"创建 Checkout 失败: {resp.status_code} - {resp.text[:300]}")

        data = resp.json()
        logger.info(f"Checkout 创建成功: cs_id={data.get('checkout_session_id', '')[:30]}...")
        return data

    def run_stripe_in_browser(
        self,
        checkout_session_id: str,
        client_secret: str,
        stripe_pk: str,
        card_number: str,
        card_exp_month: str,
        card_exp_year: str,
        card_cvc: str,
        billing_name: str,
        billing_country: str,
        billing_zip: str,
        billing_line1: str = "",
        billing_email: str = "",
        timeout: int = 120,
    ) -> dict:
        """
        在真实浏览器中加载 Stripe.js, 执行:
        1. 加载 js.stripe.com/v3/ → 自然触发 m.stripe.com/6 指纹
        2. stripe.createPaymentMethod() → 卡片 tokenization
        3. 调用 payment_pages/init → 获取 expected_amount
        4. 调用 payment_pages/confirm → 提交支付
        5. 如触发 hCaptcha → stripe.handleNextAction() 自动处理

        Returns:
            {"success": True/False, "error": "...", "pi_status": "...", ...}
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {"success": False, "error": "playwright not installed"}

        logger.info(f"[Browser] 启动 Stripe.js 全流程: cs={checkout_session_id[:25]}...")

        # 构建在浏览器中执行的 HTML + JS
        stripe_flow_html = self._build_stripe_flow_page(
            stripe_pk=stripe_pk,
            checkout_session_id=checkout_session_id,
            client_secret=client_secret,
            card_number=card_number,
            card_exp_month=card_exp_month,
            card_exp_year=card_exp_year,
            card_cvc=card_cvc,
            billing_name=billing_name,
            billing_country=billing_country,
            billing_zip=billing_zip,
            billing_line1=billing_line1,
            billing_email=billing_email,
        )

        with sync_playwright() as p:
            # ── CDP 模式: 手动启动 Chrome, 通过 CDP 连接 ──
            # Playwright connect_over_cdp 不注入自动化标记:
            #   navigator.webdriver = false
            #   无 Playwright 特有的 JS 注入
            # hCaptcha Enterprise 看到的是原生 Chrome 指纹
            chrome_path = self._find_chrome_binary()
            cdp_port = random.randint(9300, 9400)
            user_data_dir = f"/tmp/cdp-stripe-{cdp_port}"

            # 清理旧数据目录
            import shutil
            if os.path.exists(user_data_dir):
                shutil.rmtree(user_data_dir, ignore_errors=True)

            chrome_args = [
                chrome_path,
                f"--remote-debugging-port={cdp_port}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                f"--window-size=1366,900",
                f"--user-data-dir={user_data_dir}",
            ]
            # 非 headless 模式: 使用 SwiftShader 提供软件 GPU (Xvfb 无硬件 GPU)
            if not self.headless:
                chrome_args.extend([
                    "--use-gl=angle",
                    "--use-angle=swiftshader-webgl",
                    "--enable-unsafe-swiftshader",
                ])
            else:
                chrome_args.append("--disable-gpu")
            if self.proxy:
                chrome_args.append(f"--proxy-server={self.proxy}")
            if self.headless:
                chrome_args.append("--headless=new")
            chrome_args.append("about:blank")

            logger.info(f"[Browser] 启动 Chrome (CDP port={cdp_port})...")
            chrome_proc = subprocess.Popen(
                chrome_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # 等待 CDP 端口就绪
            import urllib.request
            cdp_url = f"http://127.0.0.1:{cdp_port}"
            for attempt in range(20):
                try:
                    resp = urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2)
                    version_info = json.loads(resp.read())
                    logger.info(f"[Browser] Chrome 已启动: {version_info.get('Browser', 'unknown')}")
                    break
                except Exception:
                    time.sleep(0.5)
            else:
                chrome_proc.terminate()
                return {"success": False, "error": "Chrome CDP port not responding"}

            try:
                browser = p.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0]
                page = context.new_page()
                page.set_default_timeout(timeout * 1000)

                # 如果 headless, 通过 CDP 覆盖 User-Agent 和其他标记
                if self.headless:
                    cdp_session = context.new_cdp_session(page)
                    cdp_session.send("Network.setUserAgentOverride", {
                        "userAgent": (
                            "Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/145.0.0.0 Safari/537.36"
                        ),
                        "platform": "Linux",
                    })
                    # 覆盖 navigator.webdriver 等标记
                    cdp_session.send("Page.addScriptToEvaluateOnNewDocument", {
                        "source": """
                            Object.defineProperty(navigator, 'webdriver', {get: () => false});
                            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                            Object.defineProperty(navigator, 'languages', {get: () => ['en-GB','en-US','en']});
                            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
                        """
                    })
                    logger.info("[Browser] headless 反检测补丁已应用")

                # 监听浏览器 console 日志
                page.on("console", lambda msg: logger.info(f"[Browser Console] {msg.text}"))

                # 通过 Playwright response 事件监听 m.stripe.com/6 响应, 提取 guid
                captured_guid = {"value": ""}

                def on_response(response):
                    if "m.stripe.com/6" in response.url:
                        try:
                            data = response.json()
                            if data.get("guid"):
                                captured_guid["value"] = data["guid"]
                                logger.info(f"[Intercept] guid captured: {data['guid'][:20]}...")
                        except Exception as e:
                            logger.debug(f"[Intercept] m.stripe.com/6 parse failed: {e}")

                page.on("response", on_response)

                # 拦截 checkout.stripe.com 域, 注入我们的支付页面
                # checkout.stripe.com 有 CORS 权限访问 api.stripe.com
                # 且对 hCaptcha 来说是合法的 Stripe 域
                page.route("https://checkout.stripe.com/v3/payment*", lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=stripe_flow_html,
                ))

                logger.info("[Browser] 导航到 checkout.stripe.com...")
                page.goto("https://checkout.stripe.com/v3/payment", wait_until="domcontentloaded", timeout=15000)

                # 等待 Stripe.js 加载完成
                logger.info("[Browser] 等待 Stripe.js 加载...")
                page.wait_for_function("window.__stripeReady === true", timeout=30000)
                logger.info("[Browser] Stripe.js 已加载")

                # 等待 Stripe Elements 渲染完成
                logger.info("[Browser] 等待 Stripe Elements 渲染...")
                page.wait_for_function("window.__elementsReady === true", timeout=15000)
                logger.info("[Browser] Elements 已就绪")

                # 模拟用户行为 (提升指纹质量)
                self._simulate_human_behavior(page)
                time.sleep(random.uniform(1.0, 2.0))

                # 填写 Stripe Elements iframe 中的卡信息
                # 点击 iframe 会触发 m.stripe.com/6 指纹采集
                logger.info("[Browser] 填写卡信息到 Stripe Elements...")
                self._fill_stripe_elements(page, card_number, card_exp_month, card_exp_year, card_cvc)

                time.sleep(random.uniform(1.0, 2.0))

                # 等待 guid 捕获 (m.stripe.com/6 在 iframe 获得焦点后~20秒后才发起)
                logger.info("[Browser] 等待 guid 捕获...")
                guid_wait_start = time.time()
                while not captured_guid["value"] and (time.time() - guid_wait_start) < 25:
                    time.sleep(0.3)

                if captured_guid["value"]:
                    page.evaluate(f'window.__stripeGuid = "{captured_guid["value"]}"')
                    logger.info(f"[Browser] guid 已注入: {captured_guid['value'][:30]}...")
                else:
                    logger.warning("[Browser] guid 未捕获, confirm 将不含 guid")

                # 截图
                page.screenshot(path="test_outputs/browser_stripe_loaded.png")

                # 执行支付流程 (异步) + 自动点击 hCaptcha
                logger.info("[Browser] 执行支付流程...")

                # 用 addScriptTag 启动异步支付 (不会阻塞 Python)
                page.add_script_tag(content="""
                    window.__payResult = null;
                    window.__payDone = false;
                    (async () => {
                        try {
                            window.__payResult = await window.__runPayment();
                        } catch(e) {
                            window.__payResult = { success: false, error: e.message || String(e) };
                        }
                        window.__payDone = true;
                    })();
                """)

                # 在等待支付结果的同时, 监控并自动点击 hCaptcha
                logger.info("[Browser] 等待支付结果 (同时监控 hCaptcha)...")
                hcaptcha_clicked = False
                pay_start = time.time()

                while (time.time() - pay_start) < timeout:
                    # 检查支付是否完成
                    try:
                        done = page.evaluate("window.__payDone === true")
                        if done:
                            break
                    except Exception:
                        pass

                    # 尝试找到并点击 hCaptcha checkbox
                    if not hcaptcha_clicked:
                        try:
                            hcaptcha_clicked = self._try_click_hcaptcha(page)
                            if hcaptcha_clicked:
                                logger.info("[Browser] hCaptcha checkbox 已自动点击!")
                        except Exception as e:
                            logger.debug(f"[Browser] hCaptcha check error: {e}")

                    time.sleep(0.5)

                # 获取结果
                result = page.evaluate("window.__payResult")
                if result is None:
                    result = {"success": False, "error": "Payment timeout"}

                logger.info(f"[Browser] 支付结果: {json.dumps(result, default=str)[:500]}")

                # 如果 hCaptcha 超时, 尝试用打码平台解决
                if result.get("step") == "hcaptcha_timeout" and result.get("hcaptcha_challenge"):
                    challenge = result["hcaptcha_challenge"]
                    # 记录当前所有 frame URL (帮助诊断 hCaptcha site_url)
                    frame_urls = [f.url for f in page.frames]
                    logger.info(f"[Browser] 所有 frame URLs: {frame_urls}")
                    logger.info("[Browser] hCaptcha challenge, 尝试打码平台...")
                    solve_result = self._solve_hcaptcha_via_service(
                        page=page,
                        challenge=challenge,
                        stripe_pk=stripe_pk,
                    )
                    if solve_result:
                        result = solve_result

                # 截图结果
                page.screenshot(path="test_outputs/browser_stripe_result.png")

                return result

            except Exception as e:
                error_msg = str(e)
                logger.error(f"[Browser] 异常: {error_msg}")
                try:
                    page.screenshot(path="test_outputs/browser_stripe_error.png")
                except Exception:
                    pass
                return {"success": False, "error": f"Browser exception: {error_msg}"}
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                chrome_proc.terminate()
                try:
                    chrome_proc.wait(timeout=5)
                except Exception:
                    chrome_proc.kill()
                # 清理用户数据目录
                import shutil
                shutil.rmtree(user_data_dir, ignore_errors=True)

    def _build_stripe_flow_page(self, stripe_pk, checkout_session_id, client_secret,
                                card_number, card_exp_month, card_exp_year, card_cvc,
                                billing_name, billing_country, billing_zip,
                                billing_line1, billing_email) -> str:
        """
        构建一个 HTML 页面, 加载真实 Stripe.js + Elements,
        让用户在 Stripe Elements iframe 中输入卡信息。
        """
        config = json.dumps({
            "pk": stripe_pk,
            "csId": checkout_session_id,
            "clientSecret": client_secret,
            "billing": {
                "name": billing_name,
                "country": billing_country,
                "postal_code": billing_zip,
                "line1": billing_line1,
                "email": billing_email,
            },
        })

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Payment Processing</title>
    <script src="https://js.stripe.com/v3/"></script>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 40px; background: #f6f9fc; }}
        .container {{ max-width: 480px; margin: 0 auto; background: white; border-radius: 8px; padding: 32px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        #status {{ margin: 16px 0; padding: 12px; border-radius: 4px; font-size: 14px; }}
        .info {{ background: #e3f2fd; color: #1565c0; }}
        .success {{ background: #e8f5e9; color: #2e7d32; }}
        .error {{ background: #fce4ec; color: #c62828; }}
        .element-container {{ padding: 12px; border: 1px solid #e0e0e0; border-radius: 4px; margin: 8px 0; }}
        label {{ display: block; font-size: 13px; color: #6b7c93; margin-bottom: 4px; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Payment</h2>
        <div id="status" class="info">Loading Stripe.js...</div>
        <label>Card Number</label>
        <div id="card-number" class="element-container"></div>
        <label>Expiry</label>
        <div id="card-expiry" class="element-container"></div>
        <label>CVC</label>
        <div id="card-cvc" class="element-container"></div>
    </div>
    <script>
        window.__stripeReady = false;
        window.__elementsReady = false;
        window.__stripeGuid = '';

        const CONFIG = {config};

        // 拦截 postMessage 捕获 Stripe m.stripe.com/6 iframe 返回的 guid
        (function() {{
            const origPostMessage = window.addEventListener;
            window.addEventListener('message', function(e) {{
                try {{
                    const data = typeof e.data === 'string' ? JSON.parse(e.data) : e.data;
                    // Stripe m.stripe.com iframe 通过 postMessage 发回 guid/muid/sid
                    if (data && data.guid) {{
                        window.__stripeGuid = data.guid;
                        console.log('[Fingerprint] guid from postMessage: ' + data.guid.substring(0, 20));
                    }}
                    if (data && data.muid) window.__stripeMuid = data.muid;
                    if (data && data.sid) window.__stripeSid = data.sid;
                }} catch(e) {{}}
            }}, true);
        }})();

        // 初始化 Stripe.js + Elements
        const stripe = Stripe(CONFIG.pk);
        const elements = stripe.elements();

        const cardNumber = elements.create('cardNumber', {{
            style: {{ base: {{ fontSize: '16px', color: '#32325d' }} }}
        }});
        const cardExpiry = elements.create('cardExpiry', {{
            style: {{ base: {{ fontSize: '16px', color: '#32325d' }} }}
        }});
        const cardCvc = elements.create('cardCvc', {{
            style: {{ base: {{ fontSize: '16px', color: '#32325d' }} }}
        }});

        cardNumber.mount('#card-number');
        cardExpiry.mount('#card-expiry');
        cardCvc.mount('#card-cvc');

        window.__stripeReady = true;

        // 等待 Elements 完全加载
        let readyCount = 0;
        function checkReady() {{
            readyCount++;
            if (readyCount >= 3) {{
                window.__elementsReady = true;
                document.getElementById('status').textContent = 'Card fields ready. Fill them in...';
            }}
        }}
        cardNumber.on('ready', checkReady);
        cardExpiry.on('ready', checkReady);
        cardCvc.on('ready', checkReady);

        // 支付流程: 创建 PM → init → confirm → handleNextAction
        // 使用 Stripe Custom Checkout API (initCustomCheckout) 实现
        window.__runPayment = async function() {{
            const statusEl = document.getElementById('status');

            try {{
                // Step 1: 用 Custom Checkout API (这会让 Stripe.js 自动处理指纹和 hCaptcha)
                console.log('[Pay] Step 1: initCustomCheckout...');
                statusEl.textContent = 'Step 1: Initializing Custom Checkout...';

                let checkout;
                try {{
                    checkout = await stripe.initCustomCheckout({{
                        clientSecret: CONFIG.clientSecret,
                    }});
                    console.log('[Pay] Custom Checkout initialized');
                }} catch(e) {{
                    console.log('[Pay] initCustomCheckout failed: ' + e.message + ', falling back to manual flow');
                    // Fall back to manual flow
                    return await window.__runPaymentManual();
                }}

                // Step 2: 用 Custom Checkout 的方法确认支付
                statusEl.textContent = 'Step 2: Confirming via Custom Checkout...';

                // 使用 checkout.confirm() 配合 Elements
                const confirmResult = await checkout.confirm({{
                    paymentMethod: {{
                        card: cardNumber,
                        billing_details: {{
                            name: CONFIG.billing.name,
                            email: CONFIG.billing.email || undefined,
                            address: {{
                                country: CONFIG.billing.country,
                                postal_code: CONFIG.billing.postal_code,
                                line1: CONFIG.billing.line1 || undefined,
                            }},
                        }},
                    }},
                    return_url: 'https://chatgpt.com/',
                }});

                console.log('[Pay] confirm result: ' + JSON.stringify(confirmResult).substring(0, 500));

                if (confirmResult.error) {{
                    statusEl.textContent = '❌ Error: ' + confirmResult.error.message;
                    statusEl.className = 'error';
                    return {{ success: false, error: 'checkout.confirm: ' + confirmResult.error.message }};
                }}

                statusEl.textContent = '✅ Payment completed!';
                statusEl.className = 'success';
                return {{ success: true, ...confirmResult }};

            }} catch(e) {{
                console.log('[Pay] error: ' + e.message);
                statusEl.textContent = '❌ Error: ' + e.message;
                statusEl.className = 'error';
                return {{ success: false, error: e.message || String(e) }};
            }}
        }};

        // 手动流程 (如果 Custom Checkout API 不可用)
        window.__runPaymentManual = async function() {{
            const statusEl = document.getElementById('status');

            try {{
                // Step 1: createPaymentMethod (用 Elements)
                console.log('[Pay] Step 1: createPaymentMethod...');
                statusEl.textContent = 'Step 1: Creating payment method...';
                statusEl.className = 'info';

                const pmResult = await stripe.createPaymentMethod({{
                    type: 'card',
                    card: cardNumber,
                    billing_details: {{
                        name: CONFIG.billing.name,
                        email: CONFIG.billing.email || undefined,
                        address: {{
                            country: CONFIG.billing.country,
                            postal_code: CONFIG.billing.postal_code,
                            line1: CONFIG.billing.line1 || undefined,
                        }},
                    }},
                }});

                if (pmResult.error) {{
                    statusEl.textContent = 'PM Error: ' + pmResult.error.message;
                    statusEl.className = 'error';
                    return {{ success: false, error: 'createPaymentMethod: ' + pmResult.error.message, step: 'paymentMethod' }};
                }}

                const pmId = pmResult.paymentMethod.id;
                console.log('[Pay] PM created: ' + pmId);
                statusEl.textContent = 'Step 1 OK: PM=' + pmId.substring(0, 20) + '...';

                // Step 2: payment_pages/init
                statusEl.textContent = 'Step 2: Initializing payment page...';

                const initResp = await fetch('https://api.stripe.com/v1/payment_pages/' + CONFIG.csId + '/init', {{
                    method: 'POST',
                    headers: {{
                        'Authorization': 'Bearer ' + CONFIG.pk,
                        'Content-Type': 'application/x-www-form-urlencoded',
                    }},
                    body: 'key=' + encodeURIComponent(CONFIG.pk) + '&browser_locale=en',
                }});
                const initData = await initResp.json();

                if (!initResp.ok) {{
                    return {{ success: false, error: 'init failed: ' + (initData.error?.message || JSON.stringify(initData).substring(0,200)), step: 'init' }};
                }}

                const eid = initData.eid || '';
                const initChecksum = initData.init_checksum || '';
                const baseDue = initData.total_summary?.due || 0;

                // 计算含税金额
                const taxRates = {{ 'GB': 0.20, 'US': 0.00, 'DE': 0.19, 'FR': 0.20, 'JP': 0.10 }};
                const taxRate = taxRates[CONFIG.billing.country] || 0.00;
                const autoTax = initData.tax_context?.automatic_tax_enabled || false;
                const requiresLocation = initData.tax_meta?.status === 'requires_location_inputs';
                let expectedAmount = baseDue;
                if (autoTax && requiresLocation) {{
                    expectedAmount = Math.round(baseDue * (1 + taxRate));
                }}

                console.log('[Pay] Step 2 OK: amount=' + expectedAmount + ' eid=' + eid.substring(0,10));
                statusEl.textContent = 'Step 2 OK: amount=' + expectedAmount;

                // Step 3: payment_pages/confirm
                console.log('[Pay] Step 3: confirm...');
                statusEl.textContent = 'Step 3: Confirming payment...';

                const confirmBody = new URLSearchParams();
                confirmBody.append('payment_method', pmId);
                confirmBody.append('expected_amount', expectedAmount.toString());
                confirmBody.append('key', CONFIG.pk);
                if (eid) confirmBody.append('eid', eid);
                if (initChecksum) confirmBody.append('init_checksum', initChecksum);

                // 从 Cookie 中提取 Stripe 指纹 (由 Stripe.js m.stripe.com/6 设置)
                const cookies = document.cookie.split(';').reduce((acc, c) => {{
                    const [k, v] = c.trim().split('=');
                    acc[k] = v;
                    return acc;
                }}, {{}});
                const muid = window.__stripeMuid || cookies['__stripe_mid'] || '';
                const sid = window.__stripeSid || cookies['__stripe_sid'] || '';
                const guid = window.__stripeGuid || '';
                if (guid) confirmBody.append('guid', guid);
                if (muid) confirmBody.append('muid', muid);
                if (sid) confirmBody.append('sid', sid);
                console.log('[Pay] fingerprints: guid=' + guid.substring(0,12) + ' muid=' + muid.substring(0,12) + ' sid=' + sid.substring(0,12));

                const confirmResp = await fetch('https://api.stripe.com/v1/payment_pages/' + CONFIG.csId + '/confirm', {{
                    method: 'POST',
                    headers: {{
                        'Authorization': 'Bearer ' + CONFIG.pk,
                        'Content-Type': 'application/x-www-form-urlencoded',
                    }},
                    body: confirmBody.toString(),
                }});
                const confirmData = await confirmResp.json();

                if (!confirmResp.ok) {{
                    return {{ success: false, error: 'confirm failed (' + confirmResp.status + '): ' + (confirmData.error?.message || ''), step: 'confirm', response: confirmData }};
                }}

                const sessionStatus = confirmData.status || '';
                const pi = confirmData.payment_intent || {{}};
                const piStatus = pi.status || '';
                console.log('[Pay] confirm result: session=' + sessionStatus + ' pi=' + piStatus);

                if (sessionStatus === 'complete' || piStatus === 'succeeded') {{
                    statusEl.textContent = '✅ Payment succeeded!';
                    statusEl.className = 'success';
                    return {{ success: true, session_status: sessionStatus, pi_status: piStatus }};
                }}

                // hCaptcha 验证?
                if (piStatus === 'requires_action') {{
                    const nextAction = pi.next_action || {{}};
                    const sdkInfo = nextAction.use_stripe_sdk || {{}};
                    const challengeType = sdkInfo.type || '';
                    console.log('[Pay] next_action: ' + JSON.stringify(nextAction).substring(0, 500));

                    if (challengeType === 'intent_confirmation_challenge') {{
                        const piClientSecret = pi.client_secret;
                        const stripeJsInfo = sdkInfo.stripe_js || {{}};
                        // stripe_js 可能是对象或字符串
                        const hcSiteKey = stripeJsInfo.site_key || sdkInfo.hcaptcha_site_key || '';
                        const hcRqdata = stripeJsInfo.rqdata || sdkInfo.hcaptcha_rqdata || '';
                        const verificationUrl = stripeJsInfo.verification_url || '';

                        console.log('[Pay] hCaptcha challenge: siteKey=' + hcSiteKey + ' rqdata=' + (hcRqdata || '').substring(0,30) + ' verifyUrl=' + verificationUrl);

                        // 存储 challenge 信息供 Python 使用
                        window.__hcaptchaChallenge = {{
                            pi_client_secret: piClientSecret,
                            site_key: hcSiteKey,
                            rqdata: hcRqdata,
                            verification_url: verificationUrl,
                            pi_id: pi.id || '',
                        }};

                        // headless 模式: 跳过 handleNextAction, 直接返回 challenge 信息给 Python 处理
                        if (window.__skipHandleNextAction) {{
                            console.log('[Pay] headless 模式: 跳过 handleNextAction, 由 Python 处理 hCaptcha');
                            return {{
                                success: false,
                                error: 'hcaptcha_challenge_detected',
                                step: 'hcaptcha_timeout',
                                pi_status: piStatus,
                                hcaptcha_challenge: window.__hcaptchaChallenge,
                            }};
                        }}

                        // headed 模式: 用 handleNextAction 自动解决 (有30秒超时)
                        if (!piClientSecret) {{
                            return {{ success: false, error: 'No PI client_secret', step: 'hcaptcha', hcaptcha_challenge: window.__hcaptchaChallenge }};
                        }}

                        console.log('[Pay] handleNextAction starting... (timeout=30s)');
                        // 加超时: 如果 60s 内 handleNextAction 没完成, 自动放弃
                        const handlePromise = stripe.handleNextAction({{
                            clientSecret: piClientSecret,
                        }});
                        const timeoutPromise = new Promise((_, reject) =>
                            setTimeout(() => reject(new Error('handleNextAction_timeout')), 60000)
                        );
                        let handleResult;
                        try {{
                            handleResult = await Promise.race([handlePromise, timeoutPromise]);
                        }} catch(timeoutErr) {{
                            console.log('[Pay] handleNextAction timed out (60s)');
                            return {{
                                success: false,
                                error: 'hcaptcha_timeout',
                                step: 'hcaptcha_timeout',
                                pi_status: piStatus,
                                hcaptcha_challenge: window.__hcaptchaChallenge,
                            }};
                        }}
                        console.log('[Pay] handleNextAction completed, error=' + (handleResult.error?.message || 'none'));

                        if (handleResult.error) {{
                            statusEl.textContent = '❌ hCaptcha failed: ' + handleResult.error.message;
                            statusEl.className = 'error';
                            return {{
                                success: false,
                                error: 'handleNextAction: ' + handleResult.error.message,
                                step: 'hcaptcha',
                                pi_status: piStatus,
                            }};
                        }}

                        const handledPI = handleResult.paymentIntent || {{}};
                        const handledStatus = handledPI.status || '';

                        if (handledStatus === 'succeeded' || handledStatus === 'processing') {{
                            statusEl.textContent = '✅ Payment succeeded (after hCaptcha)!';
                            statusEl.className = 'success';
                            return {{ success: true, pi_status: handledStatus, hcaptcha_handled: true }};
                        }}

                        return {{
                            success: false,
                            error: 'After handleNextAction: pi_status=' + handledStatus,
                            step: 'post_hcaptcha',
                            pi_status: handledStatus,
                            last_error: handledPI.last_payment_error || null,
                        }};
                    }}

                    return {{ success: false, error: 'Unknown action: ' + challengeType, step: 'action' }};
                }}

                return {{ success: false, error: 'Unexpected: session=' + sessionStatus + ' pi=' + piStatus, step: 'confirm' }};

            }} catch(e) {{
                statusEl.textContent = '❌ Error: ' + e.message;
                statusEl.className = 'error';
                return {{ success: false, error: e.message || String(e) }};
            }}
        }};
    </script>
</body>
</html>"""

    def _solve_hcaptcha_via_service(self, page, challenge: dict, stripe_pk: str) -> dict | None:
        """
        当 handleNextAction 超时 (headless 模式), 用打码平台解决 hCaptcha,
        然后通过 Stripe verify_challenge API 提交 token。
        """
        api_site_key = challenge.get("site_key", "")
        rqdata = challenge.get("rqdata", "")
        verification_url = challenge.get("verification_url", "")
        pi_client_secret = challenge.get("pi_client_secret", "")
        pi_id = challenge.get("pi_id", "")

        if not api_site_key or not verification_url:
            logger.error(f"[Solver] challenge 参数不完整: site_key={bool(api_site_key)} verifyUrl={bool(verification_url)}")
            return None

        # 从浏览器 frame 中提取真实的 hCaptcha sitekey (可能与 API 返回的不同)
        real_site_key = api_site_key
        real_site_url = "https://b.stripecdn.com"  # hCaptcha host domain from frame analysis
        try:
            for frame in page.frames:
                url = frame.url
                if "newassets.hcaptcha.com" in url and "sitekey=" in url:
                    import urllib.parse
                    fragment = url.split("#", 1)[-1] if "#" in url else ""
                    params = dict(p.split("=", 1) for p in fragment.split("&") if "=" in p)
                    sk = params.get("sitekey", "")
                    origin = urllib.parse.unquote(params.get("origin", ""))
                    if sk and sk != api_site_key:
                        real_site_key = sk
                        logger.info(f"[Solver] 使用 frame sitekey={real_site_key} (不同于 API sitekey)")
                    if origin:
                        real_site_url = origin
                    logger.info(f"[Solver] hCaptcha frame: sitekey={real_site_key[:20]}... origin={real_site_url}")
                    break
        except Exception as e:
            logger.warning(f"[Solver] 提取 frame info 失败: {e}")

        logger.info(f"[Solver] 开始打码: sitekey={real_site_key[:20]}... site_url={real_site_url} rqdata={bool(rqdata)}")

        # YesCaptcha 配置
        YESCAPTCHA_KEY = "27e2aa9da9a236b2a6cfcc3fa0f045fdec2a3633104361"
        from captcha_solver import CaptchaSolver
        solver = CaptchaSolver(
            api_url="https://api.yescaptcha.com",
            client_key=YESCAPTCHA_KEY,
        )

        captcha_result = solver.solve_hcaptcha(
            site_key=real_site_key,
            site_url=real_site_url,
            rqdata=rqdata,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
            timeout=120,
            is_invisible=True,
        )

        if not captcha_result:
            logger.error("[Solver] 打码失败")
            return None

        token = captcha_result["token"]
        ekey = captcha_result.get("ekey", "")
        logger.info(f"[Solver] 打码成功, token 长度: {len(token)}, ekey: {bool(ekey)}")

        # 通过 Python requests 调用 verify_challenge (使用正确的 Origin 和 Referer)
        verify_full_url = f"https://api.stripe.com{verification_url}" if verification_url.startswith("/") else verification_url
        import requests as req
        headers = {
            "Authorization": f"Bearer {stripe_pk}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
            ),
        }
        form_data = {
            "challenge_response_token": token,
            "challenge_response_ekey": ekey,
            "key": stripe_pk,
        }
        if pi_client_secret:
            form_data["client_secret"] = pi_client_secret

        # 走代理 (与浏览器一致)
        proxies = {"https": self.proxy, "http": self.proxy} if self.proxy else None
        try:
            resp = req.post(verify_full_url, headers=headers, data=form_data, proxies=proxies, timeout=30)
            data = resp.json()
            verify_result = {"status": resp.status_code, "data": data}
        except Exception as e:
            verify_result = {"error": str(e)}

        logger.info(f"[Solver] verify_challenge 结果: {json.dumps(verify_result, default=str)[:500]}")

        if not verify_result or verify_result.get("error"):
            return {"success": False, "error": f"verify_challenge fetch error: {verify_result}", "step": "verify_challenge"}

        status = verify_result.get("status", 0)
        data = verify_result.get("data", {})

        if status != 200:
            err_msg = data.get("error", {}).get("message", str(data)[:200])
            return {"success": False, "error": f"verify_challenge {status}: {err_msg}", "step": "verify_challenge"}

        pi_status = data.get("status", "")
        logger.info(f"[Solver] verify_challenge 后 PI 状态: {pi_status}")

        if pi_status in ("succeeded", "processing"):
            return {"success": True, "pi_status": pi_status, "hcaptcha_solved": True}
        elif pi_status == "requires_action":
            # 可能需要再来一轮 hCaptcha
            return {"success": False, "error": f"verify_challenge: still requires_action", "step": "verify_challenge_again"}
        elif pi_status == "requires_payment_method":
            # 卡被拒绝
            last_err = data.get("last_payment_error", {})
            return {"success": False, "error": f"Card declined: {last_err.get('message', '')}", "step": "card_declined", "hcaptcha_solved": True}
        else:
            return {"success": False, "error": f"verify_challenge: pi_status={pi_status}", "step": "verify_challenge"}

    def _try_click_hcaptcha(self, page) -> bool:
        """
        检测并自动点击 hCaptcha checkbox。
        handleNextAction 创建的 hCaptcha 结构:
        - js.stripe.com/v3/hcaptcha-inner-*.html (sitekey=c7faac4c...)
          - b.stripecdn.com/.../HCaptcha.html
            - newassets.hcaptcha.com/...#frame=checkbox (这里有 checkbox!)
            - newassets.hcaptcha.com/...#frame=challenge
        使用 page.frames 遍历所有嵌套 frame 定位 checkbox。
        """
        for frame in page.frames:
            url = frame.url
            # 找到 hCaptcha checkbox frame (frame=checkbox, NOT checkbox-invisible)
            if "newassets.hcaptcha.com" not in url:
                continue
            if "frame=checkbox&" not in url and not url.endswith("frame=checkbox"):
                continue
            # 跳过 invisible checkbox
            if "checkbox-invisible" in url:
                continue

            logger.info(f"[hCaptcha] 发现 checkbox frame: {url[:80]}...")

            try:
                # hCaptcha checkbox 的 ID 是 #checkbox
                checkbox = frame.query_selector('#checkbox')
                if checkbox:
                    checkbox.click()
                    logger.info("[hCaptcha] checkbox 已点击!")
                    return True
                # 备选选择器
                for sel in ['.check', '[role="checkbox"]', '#anchor']:
                    el = frame.query_selector(sel)
                    if el:
                        el.click()
                        logger.info(f"[hCaptcha] 点击了 {sel}")
                        return True
                # 最后尝试: 点击 frame 中心
                el = frame.query_selector('body')
                if el:
                    box = el.bounding_box()
                    if box:
                        page.mouse.click(box['x'] + box['width']/2, box['y'] + box['height']/2)
                        logger.info("[hCaptcha] 点击了 checkbox frame body 中心")
                        return True
            except Exception as e:
                logger.debug(f"[hCaptcha] checkbox frame 点击失败: {e}")

        return False

    @staticmethod
    def _find_chrome_binary() -> str:
        """查找可用的 Chrome/Chromium 二进制文件"""
        # 优先使用 Playwright 自带的 Chrome for Testing (已验证可 CDP)
        pw_chrome = os.path.expanduser("~/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome")
        if os.path.isfile(pw_chrome):
            return pw_chrome

        # 系统 Chrome
        for path in [
            "/opt/google/chrome/chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]:
            if os.path.isfile(path):
                return path

        # Playwright 其他版本
        import glob as gl
        pw_chromes = gl.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
        if pw_chromes:
            return sorted(pw_chromes)[-1]

        raise FileNotFoundError("No Chrome/Chromium binary found")

    def _simulate_human_behavior(self, page):
        """模拟人类浏览行为"""
        for _ in range(random.randint(3, 6)):
            x = random.randint(200, 900)
            y = random.randint(100, 600)
            page.mouse.move(x, y, steps=random.randint(8, 20))
            time.sleep(random.uniform(0.1, 0.3))
        page.evaluate("window.scrollBy(0, 200)")
        time.sleep(random.uniform(0.3, 0.6))
        page.evaluate("window.scrollBy(0, -100)")
        time.sleep(random.uniform(0.2, 0.5))

    def _fill_stripe_elements(self, page, card_number, exp_month, exp_year, cvc):
        """
        填写 Stripe Elements iframe 中的卡信息。
        Stripe Elements 使用虚拟输入 - 可见区域是 div/span, 实际 input 是 hidden。
        必须点击 iframe 可见区域获得焦点后用 keyboard.type。
        """
        # 找到 3 个 Element iframe (按 DOM 顺序: 卡号, 到期, CVC)
        iframe_elements = page.query_selector_all('div.element-container iframe, #card-number iframe, #card-expiry iframe, #card-cvc iframe')
        if len(iframe_elements) < 3:
            # 备用: 查找所有 Stripe Element iframe
            iframe_elements = page.query_selector_all('iframe[name*="__privateStripeFrame"]')
            logger.info(f"[Browser] 找到 {len(iframe_elements)} 个 Stripe iframe (fallback)")

        # 过滤: 只保留有可见区域的 iframe
        visible_iframes = []
        for iframe_el in iframe_elements:
            box = iframe_el.bounding_box()
            if box and box["width"] > 50 and box["height"] > 10:
                visible_iframes.append((iframe_el, box))
        logger.info(f"[Browser] 可见 iframe 数量: {len(visible_iframes)}")

        def type_into_iframe(iframe_info, value, field_name):
            iframe_el, box = iframe_info
            # 点击 iframe 中心位置
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            page.mouse.click(cx, cy)
            time.sleep(random.uniform(0.3, 0.5))

            # 使用 page keyboard (事件会发送到当前 focused iframe)
            page.keyboard.type(value, delay=random.randint(60, 120))
            logger.info(f"[Browser] 已输入 {field_name}")
            time.sleep(random.uniform(0.3, 0.6))

        if len(visible_iframes) >= 3:
            # 按顺序: 卡号(0), 到期(1), CVC(2)
            type_into_iframe(visible_iframes[0], card_number, "卡号")
            exp_yy = exp_year[-2:] if len(exp_year) == 4 else exp_year
            type_into_iframe(visible_iframes[1], f"{exp_month}{exp_yy}", "到期日")
            type_into_iframe(visible_iframes[2], cvc, "CVC")
        else:
            logger.error(f"[Browser] 可见 Stripe iframe 不足 3 个, 无法填写卡信息")

    def run_full_flow(
        self,
        session_token: str,
        access_token: str,
        device_id: str,
        card_number: str,
        card_exp_month: str,
        card_exp_year: str,
        card_cvc: str,
        billing_name: str,
        billing_country: str,
        billing_zip: str,
        billing_line1: str = "",
        billing_email: str = "",
        billing_currency: str = "",
        chatgpt_proxy: str = None,
        timeout: int = 120,
    ) -> dict:
        """
        完整流程: API 创建 checkout -> 浏览器中执行 Stripe 支付
        """
        # Step 1: API 创建 checkout session
        logger.info("=" * 50)
        logger.info("[Full Flow] Step 1: 创建 Checkout Session...")
        # 自动推断 currency (如果未指定)
        if not billing_currency:
            _country_currency = {
                "US": "USD", "GB": "GBP", "DE": "EUR", "FR": "EUR", "JP": "JPY",
                "SG": "SGD", "HK": "HKD", "KR": "KRW", "AU": "AUD", "CA": "CAD",
                "NL": "EUR", "IT": "EUR", "ES": "EUR", "CH": "CHF",
            }
            billing_currency = _country_currency.get(billing_country, "USD")
        checkout_data = self.create_checkout_session(
            session_token=session_token,
            access_token=access_token,
            device_id=device_id,
            chatgpt_proxy=chatgpt_proxy,
            billing_country=billing_country,
            billing_currency=billing_currency,
        )

        cs_id = checkout_data.get("checkout_session_id", "")
        client_secret = checkout_data.get("client_secret", "")
        stripe_pk = checkout_data.get("publishable_key", "")

        if not cs_id:
            return {"success": False, "error": "No checkout_session_id", "checkout_data": checkout_data}
        if not stripe_pk:
            return {"success": False, "error": "No publishable_key", "checkout_data": checkout_data}

        logger.info(f"[Full Flow] cs_id: {cs_id[:30]}..., pk: {stripe_pk[:30]}...")

        # Step 2: 浏览器中执行 Stripe 支付
        logger.info("[Full Flow] Step 2: 浏览器 Stripe.js 支付...")
        result = self.run_stripe_in_browser(
            checkout_session_id=cs_id,
            client_secret=client_secret,
            stripe_pk=stripe_pk,
            card_number=card_number,
            card_exp_month=card_exp_month,
            card_exp_year=card_exp_year,
            card_cvc=card_cvc,
            billing_name=billing_name,
            billing_country=billing_country,
            billing_zip=billing_zip,
            billing_line1=billing_line1,
            billing_email=billing_email,
            timeout=timeout,
        )

        result["checkout_data"] = checkout_data
        return result
