"""
支付流程 - Checkout + Confirm
主链路:
  1. POST /backend-api/payments/checkout  -> checkout_session_id + publishable_key
  2. 获取 Stripe 指纹 (guid/muid/sid)
  3. POST /v1/payment_methods -> 卡片 tokenization
  4. POST /v1/payment_pages/{checkout_session_id}/confirm -> 支付确认
"""
import json
import logging
import re
import uuid
from typing import Optional

from config import Config, CardInfo, BillingInfo
from auth_flow import AuthResult
from stripe_fingerprint import StripeFingerprint
from http_client import create_http_session

logger = logging.getLogger(__name__)


class PaymentResult:
    """支付结果"""

    def __init__(self):
        self.checkout_session_id: str = ""
        self.confirm_status: str = ""
        self.confirm_response: dict = {}
        self.success: bool = False
        self.error: str = ""

    def to_dict(self) -> dict:
        return {
            "checkout_session_id": self.checkout_session_id,
            "confirm_status": self.confirm_status,
            "success": self.success,
            "error": self.error,
            "confirm_response": self.confirm_response,
        }


class PaymentFlow:
    """支付协议流"""

    def __init__(self, config: Config, auth_result: AuthResult):
        self.config = config
        self.auth = auth_result
        self.session = create_http_session(proxy=config.proxy)
        self.fingerprint = StripeFingerprint(proxy=config.proxy)
        self.result = PaymentResult()
        self.stripe_pk: str = ""  # Stripe publishable key
        self.checkout_url: str = ""  # Stripe checkout URL
        self.checkout_data: dict = {}  # 完整 checkout 响应
        self.payment_method_id: str = ""  # tokenized payment method ID

        # 设置认证 cookie
        self.session.cookies.set(
            "__Secure-next-auth.session-token",
            auth_result.session_token,
            domain=".chatgpt.com",
        )
        if auth_result.device_id:
            self.session.cookies.set("oai-did", auth_result.device_id, domain=".chatgpt.com")

    def _get_sentinel_token(self) -> str:
        """获取支付场景的 sentinel token"""
        device_id = self.auth.device_id or str(uuid.uuid4())
        body = json.dumps({"p": "", "id": device_id, "flow": "authorize_continue"})
        headers = {
            "Origin": "https://sentinel.openai.com",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
            "Content-Type": "text/plain;charset=UTF-8",
        }
        resp = self.session.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            headers=headers,
            data=body,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Sentinel Token 获取失败: {resp.status_code}")
        token = resp.json().get("token", "")
        return json.dumps({
            "p": "", "t": "", "c": token, "id": device_id, "flow": "authorize_continue"
        })

    # ── Step 1: 创建 Checkout Session ──
    def create_checkout_session(self) -> str:
        """
        POST /backend-api/payments/checkout
        返回 checkout_session_id
        """
        logger.info("[支付 1/3] 创建 Checkout Session...")

        sentinel = self._get_sentinel_token()

        headers = {
            "Authorization": f"Bearer {self.auth.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "oai-device-id": self.auth.device_id,
            "openai-sentinel-token": sentinel,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

        plan = self.config.team_plan
        billing = self.config.billing

        body = {
            "plan_name": plan.plan_name,
            "team_plan_data": {
                "workspace_name": plan.workspace_name,
                "price_interval": plan.price_interval,
                "seat_quantity": plan.seat_quantity,
            },
            "billing_details": {
                "country": billing.country,
                "currency": billing.currency,
            },
            "cancel_url": f"https://chatgpt.com/?promo_campaign={plan.promo_campaign_id}#team-pricing",
            "promo_campaign": {
                "promo_campaign_id": plan.promo_campaign_id,
                "is_coupon_from_query_param": True,
            },
            "checkout_ui_mode": "custom",
        }

        resp = self.session.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            headers=headers,
            json=body,
            timeout=30,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"创建 Checkout Session 失败: {resp.status_code} - {resp.text[:300]}"
            )

        data = resp.json()
        logger.debug(f"Checkout 返回字段: {list(data.keys())}")
        logger.debug(f"Checkout 返回内容: {json.dumps(data, ensure_ascii=False)[:1500]}")

        # 保存 checkout_url 和 publishable_key
        self.checkout_url = data.get("url", "") or data.get("checkout_url", "")
        pk_from_response = data.get("publishable_key", "")
        if pk_from_response:
            self.stripe_pk = pk_from_response
            logger.info(f"Stripe PK (from checkout): {self.stripe_pk[:30]}...")

        # 保存完整 checkout 返回数据
        self.checkout_data = data

        # 从返回提取 checkout_session_id
        cs_id = (
            data.get("checkout_session_id")
            or data.get("session_id")
            or ""
        )

        # 从 checkout_url 中提取
        if not cs_id:
            checkout_url = self.checkout_url
            if "cs_" in checkout_url:
                m = re.search(r"(cs_[A-Za-z0-9_]+)", checkout_url)
                if m:
                    cs_id = m.group(1)

        # 从 client_secret 中提取
        if not cs_id:
            secret = data.get("client_secret", "")
            if secret and "_secret_" in secret:
                cs_id = secret.split("_secret_")[0]

        if not cs_id:
            raise RuntimeError(f"未能从返回中提取 checkout_session_id: {data}")

        self.result.checkout_session_id = cs_id
        logger.info(f"Checkout Session ID: {cs_id[:30]}...")
        return cs_id

    # ── Step 2: 获取 Stripe 指纹 ──
    def fetch_stripe_fingerprint(self):
        """获取 guid/muid/sid"""
        logger.info("[支付 2/4] 获取 Stripe 设备指纹...")
        self.fingerprint.fetch_from_m_stripe()

    # ── Step 2.5: 提取 Stripe publishable key ──
    def extract_stripe_pk(self, checkout_url: str) -> str:
        """
        从 checkout 页面或 payment_pages 接口提取 Stripe publishable key.
        pk_live_xxx 是公开的，嵌入在 checkout 页面中。
        """
        logger.info("[支付 3/4] 获取 Stripe Publishable Key...")

        # 如果已经从 checkout 响应中获取到了，直接返回
        if self.stripe_pk:
            logger.info(f"已有 Stripe PK: {self.stripe_pk[:30]}...")
            return self.stripe_pk

        cs_id = self.result.checkout_session_id

        # 如果没有 checkout_url，尝试构造
        if not checkout_url and cs_id:
            checkout_url = f"https://checkout.stripe.com/c/pay/{cs_id}"

        # 方法 1: 从 checkout 页面提取
        if checkout_url:
            try:
                headers = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
                    ),
                }
                resp = self.session.get(checkout_url, headers=headers, timeout=30, allow_redirects=True)
                logger.debug(f"Checkout 页面状态: {resp.status_code}, 长度: {len(resp.text)}")
                if resp.status_code == 200:
                    m = re.search(r'(pk_(?:live|test)_[A-Za-z0-9]+)', resp.text)
                    if m:
                        self.stripe_pk = m.group(1)
                        logger.info(f"Stripe PK: {self.stripe_pk[:20]}...")
                        return self.stripe_pk
                    else:
                        logger.debug(f"checkout 页面中未找到 pk_ 模式")
            except Exception as e:
                logger.warning(f"从 checkout 页面提取 PK 失败: {e}")

        # 方法 2: 从 payment_pages/{cs_id} 获取 (无需auth, 返回包含pk)
        if cs_id:
            try:
                resp = self.session.get(
                    f"https://api.stripe.com/v1/payment_pages/{cs_id}",
                    headers={"Accept": "application/json"},
                    timeout=30,
                )
                logger.debug(f"payment_pages 状态: {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    pk = data.get("merchant", {}).get("publishable_key", "")
                    if not pk:
                        # 尝试更深层查找
                        pk = data.get("publishable_key", "")
                    if pk:
                        self.stripe_pk = pk
                        logger.info(f"Stripe PK (from payment_pages): {self.stripe_pk[:20]}...")
                        return self.stripe_pk
                    else:
                        logger.debug(f"payment_pages 返回字段: {list(data.keys())}")
            except Exception as e:
                logger.warning(f"从 payment_pages 提取 PK 失败: {e}")

        # 方法 3: 从 elements/sessions 获取
        if cs_id:
            try:
                client_secret = f"{cs_id}_secret_placeholder"
                resp = self.session.get(
                    "https://api.stripe.com/v1/elements/sessions",
                    params={"client_secret": client_secret, "type": "payment_intent"},
                    headers={"Accept": "application/json"},
                    timeout=30,
                )
                logger.debug(f"elements/sessions 状态: {resp.status_code}")
            except Exception:
                pass

        raise RuntimeError("无法获取 Stripe publishable key")

    # ── Step 3: 创建支付方式 (卡片 tokenization) ──
    def create_payment_method(self) -> str:
        """
        POST /v1/payment_methods
        先将卡片信息 tokenize, 返回 pm_xxx ID
        Stripe 限制直接在 confirm 中提交原始卡号
        """
        logger.info("[支付 3.5/5] 创建 Payment Method (卡片 tokenization)...")

        card = self.config.card
        billing = self.config.billing
        fp = self.fingerprint.get_params()

        form_data = {
            "type": "card",
            "card[number]": card.number,
            "card[cvc]": card.cvc,
            "card[exp_month]": card.exp_month,
            "card[exp_year]": card.exp_year,
            "billing_details[name]": billing.name,
            "billing_details[email]": billing.email or self.auth.email,
            "billing_details[address][country]": billing.country,
            "billing_details[address][line1]": billing.address_line1,
            "billing_details[address][state]": billing.address_state,
            "billing_details[address][postal_code]": billing.postal_code,
            "allow_redisplay": "always",
            "guid": fp["guid"],
            "muid": fp["muid"],
            "sid": fp["sid"],
            "payment_user_agent": f"stripe.js/{self.config.stripe_build_hash}; stripe-js-v3/{self.config.stripe_build_hash}; checkout",
        }

        headers = {
            "Authorization": f"Bearer {self.stripe_pk}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

        resp = self.session.post(
            "https://api.stripe.com/v1/payment_methods",
            headers=headers,
            data=form_data,
            timeout=30,
        )

        if resp.status_code != 200:
            # 保存原始 Stripe 响应供 UI 展示
            try:
                self.result.confirm_response = resp.json()
            except Exception:
                self.result.confirm_response = {"raw": resp.text[:500]}
            self.result.confirm_status = str(resp.status_code)

            err = resp.text[:300]
            try:
                err = resp.json().get("error", {}).get("message", err)
            except Exception:
                pass
            raise RuntimeError(f"创建 Payment Method 失败 ({resp.status_code}): {err}")

        pm_data = resp.json()
        pm_id = pm_data.get("id", "")
        logger.info(f"Payment Method ID: {pm_id[:20]}...")
        return pm_id

    # ── Step 4: 确认支付 ──
    def confirm_payment(self, checkout_session_id: str) -> PaymentResult:
        """
        POST /v1/payment_pages/{checkout_session_id}/confirm
        使用已 tokenized 的 payment_method 确认支付
        """
        logger.info("[支付 4/5] 确认支付...")

        fp = self.fingerprint.get_params()

        # Stripe confirm 使用 application/x-www-form-urlencoded
        form_data = {
            # 使用 tokenized payment method
            "payment_method": self.payment_method_id,
            # Stripe 风控指纹
            "guid": fp["guid"],
            "muid": fp["muid"],
            "sid": fp["sid"],
            # 预期金额 (必填)
            "expected_amount": "0",
        }

        headers = {
            "Authorization": f"Bearer {self.stripe_pk}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
            ),
        }

        url = f"https://api.stripe.com/v1/payment_pages/{checkout_session_id}/confirm"
        resp = self.session.post(url, headers=headers, data=form_data, timeout=60)

        self.result.confirm_status = str(resp.status_code)
        try:
            self.result.confirm_response = resp.json()
        except Exception:
            self.result.confirm_response = {"raw": resp.text[:500]}

        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "")
            if status in ("succeeded", "complete", "requires_action"):
                self.result.success = status in ("succeeded", "complete")
                if status == "requires_action":
                    logger.warning("支付需要额外验证 (3DS)，请手动完成")
                    self.result.error = "requires_3ds_verification"
                else:
                    logger.info("支付确认成功!")
            else:
                self.result.error = f"支付状态异常: {status}"
                logger.error(self.result.error)
        else:
            error_msg = ""
            try:
                err_data = resp.json()
                error_msg = err_data.get("error", {}).get("message", resp.text[:300])
            except Exception:
                error_msg = resp.text[:300]
            self.result.error = f"支付确认失败 ({resp.status_code}): {error_msg}"
            logger.error(self.result.error)

        return self.result

    # ── 完整支付流程 ──
    def run_payment(self) -> PaymentResult:
        """执行完整支付链路: checkout -> fingerprint -> extract PK -> tokenize card -> confirm"""
        try:
            cs_id = self.create_checkout_session()
            self.fetch_stripe_fingerprint()
            self.extract_stripe_pk(self.checkout_url)
            self.payment_method_id = self.create_payment_method()
            return self.confirm_payment(cs_id)
        except Exception as e:
            self.result.error = str(e)
            logger.error(f"支付流程异常: {e}")
            return self.result
