PLATFORMS = {
    "bilibili": {
        "label": "Bilibili",
        "login_url": "https://passport.bilibili.com/login",
        "account_check_url": "https://space.bilibili.com/",
        "required_cookies": ["SESSDATA", "DedeUserID"],
        "cookie_domain": ".bilibili.com",
        "qr_login": True,
        "cookie_login": True,
    },
    "weibo": {
        "label": "Weibo",
        "login_url": "https://passport.weibo.cn/signin/login",
        "account_check_url": "https://weibo.com/",
        "required_cookies": ["SUB"],
        "cookie_domain": ".weibo.com",
        "qr_login": True,
        "cookie_login": True,
    },
    "douyin": {
        "label": "Douyin",
        "login_url": "https://www.douyin.com/passport/web/login/",
        "account_check_url": "https://www.douyin.com/",
        "required_cookies": ["sessionid"],
        "cookie_domain": ".douyin.com",
        "qr_login": True,
        "cookie_login": True,
    },
    "xiaohongshu": {
        "label": "Xiaohongshu",
        "login_url": "https://www.xiaohongshu.com/login",
        "account_check_url": "https://www.xiaohongshu.com/explore",
        "required_cookies": ["web_session"],
        "cookie_domain": ".xiaohongshu.com",
        "qr_login": True,
        "cookie_login": True,
    },
}


def get_platform(platform: str) -> dict | None:
    return PLATFORMS.get(platform)
