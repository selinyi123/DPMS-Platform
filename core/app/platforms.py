from app.adapter_config import platform_has_real_adapter


PLATFORMS = {
    "bilibili": {
        "label": "Bilibili",
        "login_url": "https://passport.bilibili.com/login",
        "account_check_url": "https://space.bilibili.com/",
        "required_cookies": ["SESSDATA", "DedeUserID"],
        "cookie_domain": ".bilibili.com",
        "qr_login": True,
        "cookie_login": True,
        "action_adapter": False,
        "adapter_status": "calibration_required",
    },
    "weibo": {
        "label": "Weibo",
        "login_url": "https://passport.weibo.cn/signin/login",
        "account_check_url": "https://weibo.com/",
        "required_cookies": ["SUB"],
        "cookie_domain": ".weibo.com",
        "qr_login": True,
        "cookie_login": True,
        "action_adapter": False,
        "adapter_status": "calibration_required",
    },
    "douyin": {
        "label": "Douyin",
        "login_url": "https://www.douyin.com/passport/web/login/",
        "account_check_url": "https://www.douyin.com/",
        "required_cookies": ["sessionid"],
        "cookie_domain": ".douyin.com",
        "qr_login": True,
        "cookie_login": True,
        "action_adapter": False,
        "adapter_status": "calibration_required",
    },
    "xiaohongshu": {
        "label": "Xiaohongshu",
        "login_url": "https://www.xiaohongshu.com/login",
        "account_check_url": "https://www.xiaohongshu.com/explore",
        "required_cookies": ["web_session"],
        "cookie_domain": ".xiaohongshu.com",
        "qr_login": True,
        "cookie_login": True,
        "action_adapter": False,
        "adapter_status": "calibration_required",
    },
}


def get_platform(platform: str) -> dict | None:
    cfg = PLATFORMS.get(platform)
    if not cfg:
        return None
    output = dict(cfg)
    if platform != "bilibili" and platform_has_real_adapter(platform):
        output["action_adapter"] = True
        output["adapter_status"] = "configured"
    return output


def get_platforms() -> dict:
    return {platform: get_platform(platform) for platform in PLATFORMS}
