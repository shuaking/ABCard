"""
自动化绑卡支付 - 配置文件
"""
import os
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MailConfig:
    """邮箱服务配置"""
    worker_domain: str = "https://apimail.mkai.de5.net"
    admin_token: str = "ma123999"
    email_domain: str = "mkai.de5.net"


@dataclass
class CardInfo:
    """信用卡信息"""
    number: str = ""
    cvc: str = ""
    exp_month: str = ""
    exp_year: str = ""


@dataclass
class BillingInfo:
    """账单信息"""
    name: str = "Taro Yamada"
    email: str = ""
    country: str = "JP"
    currency: str = "JPY"
    address_line1: str = "1-1-1 Shibuya"
    address_state: str = "Tokyo"
    postal_code: str = "150-0002"


@dataclass
class TeamPlanConfig:
    """团队计划配置"""
    plan_name: str = "chatgptteamplan"
    workspace_name: str = "Artizancloud"
    price_interval: str = "month"
    seat_quantity: int = 5
    promo_campaign_id: str = "team0dollar"


@dataclass
class Config:
    """总配置"""
    mail: MailConfig = field(default_factory=MailConfig)
    card: CardInfo = field(default_factory=CardInfo)
    billing: BillingInfo = field(default_factory=BillingInfo)
    team_plan: TeamPlanConfig = field(default_factory=TeamPlanConfig)
    proxy: Optional[str] = None
    # 已有凭证（可选，跳过注册直接支付时使用）
    session_token: Optional[str] = None
    access_token: Optional[str] = None
    device_id: Optional[str] = None
    # Stripe
    stripe_build_hash: str = "f197c9c0f0"

    @classmethod
    def from_file(cls, path: str) -> "Config":
        """从 JSON 文件加载配置"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = cls()
        if "mail" in data:
            cfg.mail = MailConfig(**data["mail"])
        if "card" in data:
            cfg.card = CardInfo(**data["card"])
        if "billing" in data:
            cfg.billing = BillingInfo(**data["billing"])
        if "team_plan" in data:
            cfg.team_plan = TeamPlanConfig(**data["team_plan"])
        cfg.proxy = data.get("proxy")
        cfg.session_token = data.get("session_token")
        cfg.access_token = data.get("access_token")
        cfg.device_id = data.get("device_id")
        cfg.stripe_build_hash = data.get("stripe_build_hash", cfg.stripe_build_hash)
        return cfg

    def to_dict(self) -> dict:
        return {
            "mail": self.mail.__dict__,
            "card": self.card.__dict__,
            "billing": self.billing.__dict__,
            "team_plan": self.team_plan.__dict__,
            "proxy": self.proxy,
            "session_token": self.session_token,
            "access_token": self.access_token,
            "device_id": self.device_id,
            "stripe_build_hash": self.stripe_build_hash,
        }
