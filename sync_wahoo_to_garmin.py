#!/usr/bin/env python3
"""
Wahoo Fitness .fit 文件自动同步到 Garmin Connect 国区账户。

工作原理：
  1. 通过 IMAP 连接邮箱，搜索 Wahoo 发来的活动通知邮件
  2. 从邮件正文中提取下载链接（完整 URL）
  3. 通过 HTTP 模拟登录 wahooligan.com（SAML SSO，无 MFA）
  4. 访问下载链接页面，找到并下载 .fit 文件
  5. 登录 Garmin Connect 国区 (connect.garmin.cn)
  6. 将 .fit 文件上传到佳明国区账户
  7. 记录已处理的活动 URL，避免重复上传

触发方式：
  - GitHub Actions 定时调度（schedule cron）
  - 手动触发（workflow_dispatch）

环境变量（通过 GitHub Secrets 配置）：
  IMAP_SERVER       邮箱 IMAP 服务器地址（如 imap.qq.com）
  IMAP_USER         邮箱地址
  IMAP_PASSWORD     邮箱密码或授权码
  WAHOO_EMAIL       Wahoo 账户邮箱
  WAHOO_PASSWORD    Wahoo 账户密码
  GARMIN_EMAIL      佳明国区账户邮箱
  GARMIN_PASSWORD   佳明国区账户密码
  WAHOO_SENDER      Wahoo 发件人域名（可选，默认 wahooligan.com）
  MAIL_FOLDER       邮箱搜索文件夹（可选，默认 INBOX）
  SYNC_DAYS         只处理最近 N 天的邮件（可选，默认 7）
  MAX_EMAILS        单次最多检查邮件数（可选，默认 100）
"""

import hashlib
import imaplib
import json
import logging
import os
import re
import sys
import email
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from garminconnect import Garmin, GarminConnectConnectionError

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("wahoo-garmin-sync")

# ---------------------------------------------------------------------------
# 配置（从环境变量读取）
# ---------------------------------------------------------------------------
IMAP_SERVER = os.getenv("IMAP_SERVER", "")
IMAP_PORT = 993
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")

WAHOO_EMAIL = os.getenv("WAHOO_EMAIL", "")
WAHOO_PASSWORD = os.getenv("WAHOO_PASSWORD", "")

GARMIN_EMAIL = os.getenv("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD", "")

# 可选配置，os.getenv 返回空串时用 or 走默认值
WAHOO_SENDER = os.getenv("WAHOO_SENDER") or "wahooligan.com"
MAIL_FOLDER = os.getenv("MAIL_FOLDER") or "INBOX"
SYNC_DAYS = int(os.getenv("SYNC_DAYS", "7"))
MAX_EMAILS = int(os.getenv("MAX_EMAILS", "100"))

WAHOO_BASE_URL = "https://wahooligan.com"

# 本地目录
DATA_DIR = Path("data")
FIT_DIR = DATA_DIR / "fit_files"
PROCESSED_FILE = DATA_DIR / "processed_workouts.json"

# 已处理的活动 URL 集合（用于去重）
PROCESSED_WORKOUT_IDS: set[str] = set()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def decode_mime_header(value: str) -> str:
    """解码 MIME 编码的邮件头。"""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def load_processed():
    """加载已处理记录。"""
    global PROCESSED_WORKOUT_IDS
    if PROCESSED_FILE.exists():
        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                PROCESSED_WORKOUT_IDS = set(data.get("processed_ids", []))
            logger.info("已加载 %d 条已处理记录", len(PROCESSED_WORKOUT_IDS))
        except Exception as e:
            logger.warning("加载已处理记录失败: %s，从头开始", e)
            PROCESSED_WORKOUT_IDS = set()
    else:
        logger.info("未找到已处理记录文件，从头开始处理")


def save_processed():
    """保存已处理记录。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"processed_ids": sorted(PROCESSED_WORKOUT_IDS)},
                f,
                ensure_ascii=False,
                indent=2,
            )
        logger.info("已保存 %d 条已处理记录", len(PROCESSED_WORKOUT_IDS))
    except Exception as e:
        logger.error("保存已处理记录失败: %s", e)


def url_to_workout_id(url: str) -> str:
    """从 URL 生成一个唯一的 workout ID 用于去重。

    使用 URL 的 MD5 哈希前 16 位，保证同一条链接只处理一次。
    """
    return hashlib.md5(url.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# IMAP 邮件搜索
# ---------------------------------------------------------------------------
def connect_imap() -> imaplib.IMAP4_SSL:
    """连接 IMAP 服务器并登录。"""
    logger.info("正在连接 IMAP 服务器 %s:%d ...", IMAP_SERVER, IMAP_PORT)
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(IMAP_USER, IMAP_PASSWORD)
    logger.info("IMAP 登录成功")
    mail.select(MAIL_FOLDER)
    return mail


def _is_recent_wahoo_email(raw_header: bytes) -> tuple[bool, str, str]:
    """检查邮件头是否属于最近的有效 Wahoo 活动邮件。

    过滤规则：
      1. From 地址包含 "wahoo"
      2. Subject 包含活动相关关键词（排除营销邮件）
      3. 日期在 SYNC_DAYS 范围内
    """
    msg = email.message_from_bytes(raw_header)

    from_addr = msg.get("From", "").lower()
    mail_date = msg.get("Date", "")
    msg_id = msg.get("Message-ID", "")
    subject = decode_mime_header(msg.get("Subject", "")).lower()

    if "wahoo" not in from_addr:
        return False, mail_date, msg_id

    # 过滤营销邮件，只处理可能含活动文件的邮件
    activity_keywords = (
        "activity file", "fit file", "file is ready",
        "training activity", "workout", "export",
    )
    if not any(kw in subject for kw in activity_keywords):
        logger.debug("跳过非活动邮件: Subject='%s'", subject[:60])
        return False, mail_date, msg_id

    if mail_date:
        try:
            dt = parsedate_to_datetime(mail_date)
            cutoff = datetime.now(UTC) - timedelta(days=SYNC_DAYS)
            if dt < cutoff:
                return False, mail_date, msg_id
        except Exception:
            pass

    return True, mail_date, msg_id


def search_wahoo_emails(mail: imaplib.IMAP4_SSL) -> list[tuple[str, bytes]]:
    """搜索 Wahoo 活动邮件，返回 [(email_id, raw_email), ...]。

    策略：先尝试 FROM 搜索，如果返回 0 封则退回到 ALL 搜索 + 本地过滤。
    """
    wahoo_sender = WAHOO_SENDER
    logger.info("正在获取 Wahoo 邮件列表...")

    email_ids = []

    # 策略 1: FROM 搜索
    try:
        status, data = mail.search(None, f'(FROM "{wahoo_sender}")')
        if status == "OK" and data[0]:
            email_ids = data[0].split()
            logger.info("FROM 搜索 '%s' 返回 %d 封邮件", wahoo_sender, len(email_ids))
    except Exception as e:
        logger.warning("FROM 搜索失败: %s", e)

    # 策略 2: 如果 FROM 搜索返回 0 封，退回到 ALL 搜索 + 本地过滤
    if not email_ids:
        logger.warning("FROM 搜索 '%s' 返回 0 封，退回到 ALL 搜索 + 本地过滤...", wahoo_sender)
        status, data = mail.search(None, "ALL")
        if status != "OK" or not data[0]:
            logger.error("ALL 搜索失败")
            return []

        all_ids = data[0].split()
        logger.info("邮箱共有 %d 封邮件", len(all_ids))

        # 只检查最新的 MAX_EMAILS 封（按 ID 倒序）
        all_ids_sorted = sorted(all_ids, key=lambda x: int(x), reverse=True)[:MAX_EMAILS]
        logger.info("扫描最新的 %d 封（共 %d 封）", len(all_ids_sorted), len(all_ids))

        # 通过邮件头过滤
        for eid in all_ids_sorted:
            try:
                status, header_data = mail.fetch(
                    eid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
                )
                if status != "OK" or not header_data or not header_data[0]:
                    continue

                raw_header = header_data[0][1] if isinstance(header_data[0], tuple) else b""
                if not raw_header:
                    continue

                is_wahoo, mail_date, msg_id = _is_recent_wahoo_email(raw_header)
                if is_wahoo:
                    logger.info("发现 Wahoo 活动邮件: ID=%s, Date=%s", eid.decode(), mail_date)
                    email_ids.append(eid)
            except Exception as e:
                logger.warning("检查邮件 %s 失败: %s", eid.decode(), e)

        return _fetch_full_emails(mail, email_ids)

    # FROM 搜索有结果：按日期倒序，限制数量
    email_ids = sorted(email_ids, key=lambda x: int(x), reverse=True)[:MAX_EMAILS]

    # 对 FROM 搜索的结果也做 Subject 过滤（排除营销邮件）
    filtered_ids = []
    for eid in email_ids:
        try:
            status, header_data = mail.fetch(
                eid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])"
            )
            if status != "OK" or not header_data or not header_data[0]:
                continue

            raw_header = header_data[0][1] if isinstance(header_data[0], tuple) else b""
            if not raw_header:
                continue

            is_wahoo, mail_date, msg_id = _is_recent_wahoo_email(raw_header)
            if is_wahoo:
                logger.info("发现 Wahoo 活动邮件: ID=%s, Date=%s", eid.decode(), mail_date)
                filtered_ids.append(eid)
        except Exception as e:
            logger.warning("检查邮件 %s 失败: %s", eid.decode(), e)

    return _fetch_full_emails(mail, filtered_ids)


def _fetch_full_emails(mail: imaplib.IMAP4_SSL, email_ids: list[bytes]) -> list[tuple[str, bytes]]:
    """获取指定邮件的完整内容。"""
    result = []
    for eid in email_ids:
        try:
            status, data = mail.fetch(eid, "(RFC822)")
            if status == "OK" and data and data[0]:
                raw_email = data[0][1] if isinstance(data[0], tuple) else b""
                if raw_email:
                    result.append((eid.decode(), raw_email))
        except Exception as e:
            logger.warning("获取邮件 %s 完整内容失败: %s", eid.decode(), e)

    logger.info("共获取 %d 封 Wahoo 活动邮件的完整内容", len(result))
    return result


# ---------------------------------------------------------------------------
# 邮件解析：提取下载链接
# ---------------------------------------------------------------------------
def extract_download_urls(raw_email: bytes) -> list[str]:
    """从邮件正文中提取 Wahoo 活动下载链接。

    提取策略（按优先级）：
      1. 匹配 wahooligan.com 域名下的所有 URL
      2. 匹配任意包含 workout/activity/summary/fit/download/export 关键词的 URL
      3. 匹配邮件正文中所有 HTTP/HTTPS 链接（兜底）
    """
    msg = email.message_from_bytes(raw_email)

    subject = decode_mime_header(msg.get("Subject", ""))
    logger.info("解析邮件: Subject='%s'", subject)

    # 提取邮件正文（纯文本 + HTML）
    body_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body_parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))

    full_body = "\n".join(body_parts)

    # 策略 0: 从 HTML <a> 标签的 href 属性中提取链接
    # 邮件中的链接通常是 <a href="https://...">download.fit</a> 形式
    # BeautifulSoup 能正确从纯文本正则无法捕获的超链接中提取 href
    soup = BeautifulSoup(full_body, "html.parser")
    anchor_urls = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True).lower()
        # 只保留包含 wahooligan.com 的链接，或文本含 download/fit/export 的链接
        if "wahooligan.com" in href.lower() or any(kw in text for kw in ["download", "fit", "export"]):
            anchor_urls.append(href)
            logger.debug("HTML <a> 标签链接: href=%s, text=%s", href, text)
    if anchor_urls:
        seen = set()
        unique_urls = []
        for url in anchor_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        logger.info("从 HTML <a> 标签提取到 %d 个链接", len(unique_urls))
        for url in unique_urls:
            logger.info("  链接: %s", url)
        return unique_urls

    # 策略 1: 匹配 wahooligan.com 域名下的所有 URL
    wahoo_urls = re.findall(
        r'https?://[^\s"\'<>]*wahooligan\.com/[^\s"\'<>]+',
        full_body,
        re.IGNORECASE,
    )
    if wahoo_urls:
        seen = set()
        unique_urls = []
        for url in wahoo_urls:
            clean_url = url.rstrip(".,;)")
            if clean_url not in seen:
                seen.add(clean_url)
                unique_urls.append(clean_url)
        logger.info("从邮件中提取到 %d 个 wahooligan.com 链接", len(unique_urls))
        for url in unique_urls:
            logger.info("  链接: %s", url)
        return unique_urls

    # 策略 2: 匹配任意包含活动相关关键词的 URL
    keyword_urls = re.findall(
        r'https?://[^\s"\'<>]*(?:workout|activity|summary|fit|download|export)[^\s"\'<>]*',
        full_body,
        re.IGNORECASE,
    )
    if keyword_urls:
        seen = set()
        unique_urls = []
        for url in keyword_urls:
            clean_url = url.rstrip(".,;)")
            if clean_url not in seen:
                seen.add(clean_url)
                unique_urls.append(clean_url)
        logger.info("通过关键词匹配到 %d 个链接", len(unique_urls))
        for url in unique_urls:
            logger.info("  链接: %s", url)
        return unique_urls

    # 策略 3: 兜底 - 提取所有 HTTP/HTTPS 链接
    all_urls = re.findall(r'https?://[^\s"\'<>]+', full_body)
    if all_urls:
        seen = set()
        unique_urls = []
        for url in all_urls:
            clean_url = url.rstrip(".,;)")
            if clean_url not in seen:
                seen.add(clean_url)
                unique_urls.append(clean_url)
        logger.info("兜底提取到 %d 个链接（请人工确认是否包含 Wahoo 下载链接）", len(unique_urls))
        for url in unique_urls:
            logger.info("  链接: %s", url)
        return unique_urls

    logger.warning("邮件中未找到任何链接")
    # 保存邮件正文用于调试
    FIT_DIR.mkdir(parents=True, exist_ok=True)
    debug_file = FIT_DIR / f"debug_email_{hashlib.md5(raw_email).hexdigest()[:8]}.txt"
    with open(debug_file, "w", encoding="utf-8") as f:
        f.write(full_body)
    logger.info("邮件正文已保存到 %s 供调试", debug_file)
    return []


# ---------------------------------------------------------------------------
# Wahoo 登录与 .fit 下载
# ---------------------------------------------------------------------------
def login_wahoo(session: requests.Session) -> bool:
    """模拟 Wahoo SAML SSO 登录。

    Wahoo 登录页是 Rails 应用，表单 POST 到 /saml/auth，
    包含 authenticity_token、SAMLRequest、email、password 字段。
    """
    logger.info("正在访问 Wahoo 登录页...")

    # Step 1: GET 登录页，提取 authenticity_token 和 SAMLRequest
    resp = session.get(f"{WAHOO_BASE_URL}/login", timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # 从 meta 标签提取 CSRF token
    meta_token = soup.find("meta", {"name": "csrf-token"})
    csrf_token = meta_token["content"] if meta_token else ""

    # 从表单提取 hidden 字段
    form = soup.find("form", {"action": re.compile(r"/saml/auth")})
    if not form:
        logger.error("未找到登录表单，页面结构可能已变更")
        return False

    form_inputs = {}
    for inp in form.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        value = inp.get("value", "")
        if name:
            form_inputs[name] = value

    # 获取表单 action
    action = form.get("action", "/saml/auth")
    if action.startswith("/"):
        post_url = f"{WAHOO_BASE_URL}{action}"
    else:
        post_url = action

    logger.info("登录表单 POST 到: %s", post_url)
    logger.info("表单隐藏字段: %s", list(form_inputs.keys()))

    # Step 2: POST 登录
    form_data = dict(form_inputs)
    form_data["email"] = WAHOO_EMAIL
    form_data["password"] = WAHOO_PASSWORD

    headers = {
        "Referer": f"{WAHOO_BASE_URL}/login",
        "Origin": WAHOO_BASE_URL,
    }

    logger.info("正在提交 Wahoo 登录凭证...")
    resp = session.post(post_url, data=form_data, headers=headers, timeout=30, allow_redirects=True)

    # 检查是否登录成功
    if resp.status_code == 200:
        resp_lower = resp.text.lower()
        if "invalid email or password" in resp_lower or "log in" in resp_lower and "password" in resp_lower:
            if "invalid" in resp_lower:
                logger.error("Wahoo 登录失败：邮箱或密码错误")
                return False
            logger.warning("登录后似乎仍在登录页，但未检测到明确错误，继续尝试...")

    logger.info("Wahoo 登录成功（HTTP %d，URL: %s）", resp.status_code, resp.url)
    return True


def download_fit_from_url(session: requests.Session, page_url: str) -> tuple[str, bytes] | None:
    """访问活动页面 URL，从中找到 .fit 下载链接并下载。

    Wahoo 活动页面结构（登录后）：
      - 页面顶部：活动概览（运动类型、时间、时长等）
      - Files 区域：列出设备名称作为蓝色下载链接（如 "Wahoo ELEMNT"）
      - 每个蓝色链接指向该活动的 .fit 文件

    策略：
    1. 访问活动页面 URL
    2. 解析 HTML，提取所有 <a> 标签链接
    3. 筛选候选链接（wahooligan.com 域名下 + 可能指向文件下载）
    4. 用 HEAD 请求探测 Content-Type，找到返回 fit/octet-stream 的链接
    5. 下载该链接
    """
    logger.info("正在访问活动页面: %s", page_url)

    resp = session.get(page_url, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        logger.warning("访问活动页面失败: HTTP %d, URL: %s", resp.status_code, resp.url)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # 收集所有 <a> 链接（包括可能包含设备名称的 Files 区域链接）
    all_links = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        text = a_tag.get_text(strip=True)
        all_links.append((href, text))
        logger.debug("页面链接: href=%s, text=%s", href, text)

    logger.info("页面共有 %d 个 <a> 链接", len(all_links))

    # 筛选候选下载链接：
    # 1. 直接包含 .fit 扩展名
    # 2. wahooligan.com 域名下且 URL 模式类似文件下载
    # 3. 页面中 Files 区域的设备名称链接（需要根据页面结构识别）
    candidate_urls = []

    for href, text in all_links:
        # 直接包含 .fit
        if ".fit" in href.lower():
            candidate_urls.append(href)
            logger.info("候选 .fit 链接: %s (文本: %s)", href, text)
            continue

        # 包含 download/export/export_activity 等关键词
        if any(kw in href.lower() for kw in ["download", "export", ".fit"]):
            candidate_urls.append(href)
            logger.info("候选下载链接: %s (文本: %s)", href, text)
            continue

    # 如果仍无候选，尝试通过页面结构识别 Files 区域
    # 查找包含 "Files" 或 "files" 文本的元素，其附近应有下载链接
    if not candidate_urls:
        logger.info("尝试通过页面结构查找 Files 区域...")
        # 查找包含 "Files" 文本的元素
        for elem in soup.find_all(text=re.compile(r"Files", re.IGNORECASE)):
            parent = elem.parent
            # 向上查找几层，找到包含链接的容器
            for _ in range(5):
                if parent is None:
                    break
                links = parent.find_all("a", href=True)
                for a in links:
                    href = a["href"]
                    if href not in candidate_urls and not href.startswith("#"):
                        candidate_urls.append(href)
                        logger.info("从 Files 区域找到链接: %s (文本: %s)", href, a.get_text(strip=True))
                parent = parent.parent

    # 如果没有找到任何候选链接，尝试备用方案
    if not candidate_urls:
        # 备用 1: 直接在页面 URL 后追加 /download.fit
        parsed = urlparse(page_url)
        base_path = parsed.path.rstrip("/")
        candidate_urls.append(f"{parsed.scheme}://{parsed.netloc}{base_path}.fit")
        candidate_urls.append(f"{parsed.scheme}://{parsed.netloc}{base_path}/download")
        candidate_urls.append(f"{parsed.scheme}://{parsed.netloc}{base_path}/export")
        logger.info("无候选链接，尝试备用 URL: %s", candidate_urls)

    # 对候选链接逐一探测：
    # 优先用 HEAD 请求检查 Content-Type，匹配 application/octet-stream 或 fit 相关类型
    fit_link = None
    for url in candidate_urls:
        try:
            # 确保 URL 完整
            if url.startswith("/"):
                parsed = urlparse(page_url)
                full_url = f"{parsed.scheme}://{parsed.netloc}{url}"
            elif not url.startswith("http"):
                full_url = f"{page_url.rstrip('/')}/{url}"
            else:
                full_url = url

            logger.info("探测链接: %s", full_url)
            head_resp = session.head(full_url, timeout=15, allow_redirects=True)
            content_type = head_resp.headers.get("Content-Type", "")
            content_length = head_resp.headers.get("Content-Length", "0")
            logger.info("  HEAD 结果: HTTP %d, Content-Type=%s, Content-Length=%s",
                        head_resp.status_code, content_type, content_length)

            if head_resp.status_code == 200:
                # 判断是否为 .fit 文件：
                # 1. Content-Type 包含 octet-stream / fit / garmin
                # 2. Content-Length > 1KB（.fit 文件至少几百字节）
                # 3. URL 以 .fit 结尾
                is_fit = (
                    ".fit" in full_url.lower()
                    or "application/octet-stream" in content_type
                    or "fit" in content_type.lower()
                    or "application/vnd.garmin" in content_type.lower()
                )
                try:
                    size = int(content_length) if content_length else 0
                except ValueError:
                    size = 0
                if is_fit and size > 500:
                    fit_link = full_url
                    logger.info("找到 .fit 文件链接: %s (大小: %s bytes)", full_url, content_length)
                    break

                # 如果不是 fit 文件，但返回了页面（text/html），
                # 它可能是中间跳转页，继续探测下一个
                if "text/html" in content_type:
                    logger.debug("  返回 HTML 页面，跳过")
                    continue

        except Exception as e:
            logger.warning("探测链接 %s 失败: %s", url, e)
            continue

    # 如果 HEAD 没找到，但候选 URL 以 .fit 结尾，直接尝试 GET（有些服务器不支持 HEAD）
    if not fit_link:
        for url in candidate_urls:
            if ".fit" in url.lower():
                if url.startswith("/"):
                    parsed = urlparse(page_url)
                    full_url = f"{parsed.scheme}://{parsed.netloc}{url}"
                elif not url.startswith("http"):
                    full_url = f"{page_url.rstrip('/')}/{url}"
                else:
                    full_url = url
                logger.info("HEAD 未命中，直接 GET 尝试: %s", full_url)
                fit_link = full_url
                break

    if not fit_link:
        logger.error("页面中未找到 .fit 下载链接: %s", page_url)
        # 保存页面源码用于调试
        FIT_DIR.mkdir(parents=True, exist_ok=True)
        debug_file = FIT_DIR / f"debug_page_{url_to_workout_id(page_url)}.html"
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(resp.text)
        logger.info("页面源码已保存到 %s 供调试", debug_file)
        return None

    # 下载 .fit 文件
    logger.info("正在下载 .fit 文件: %s", fit_link)
    resp = session.get(fit_link, timeout=60, allow_redirects=True)
    if resp.status_code != 200:
        logger.error("下载 .fit 失败: HTTP %d", resp.status_code)
        return None

    fit_data = resp.content
    if len(fit_data) < 100:
        logger.error("下载的 .fit 文件过小 (%d bytes)，可能不是有效文件", len(fit_data))
        return None

    # 从 URL 或 header 提取文件名
    filename = f"wahoo_{url_to_workout_id(page_url)}.fit"
    content_disposition = resp.headers.get("Content-Disposition", "")
    if content_disposition:
        fname_match = re.search(r'filename[^;=\n]*=([^;\n]*)', content_disposition)
        if fname_match:
            filename = fname_match.group(1).strip('"\' ')

    logger.info("下载成功: %s (%d bytes)", filename, len(fit_data))
    return filename, fit_data


# ---------------------------------------------------------------------------
# Garmin Connect 上传
# ---------------------------------------------------------------------------
def login_garmin_cn() -> Garmin:
    """登录 Garmin Connect 国区。"""
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        raise ValueError("GARMIN_EMAIL 和 GARMIN_PASSWORD 环境变量未配置")

    logger.info("正在登录 Garmin Connect 国区 (connect.garmin.cn) ...")

    client = Garmin(
        email=GARMIN_EMAIL,
        password=GARMIN_PASSWORD,
        is_cn=True,
    )

    try:
        client.login()
        logger.info("Garmin Connect 国区登录成功！")
    except Exception as e:
        logger.error("Garmin 登录失败: %s", e)
        raise

    return client


def upload_fit_to_garmin(client: Garmin, filename: str, fit_data: bytes) -> bool:
    """将 .fit 文件上传到 Garmin Connect。"""
    FIT_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = FIT_DIR / filename

    try:
        with open(temp_path, "wb") as f:
            f.write(fit_data)

        logger.info("正在上传 %s 到 Garmin Connect 国区 ...", filename)
        result = client.upload_activity(str(temp_path))

        if result:
            logger.info("上传成功！文件: %s", filename)
            return True
        else:
            logger.warning("上传返回空结果，文件: %s", filename)
            return False

    except GarminConnectConnectionError as e:
        error_msg = str(e).lower()

        if "409" in error_msg or "already exists" in error_msg or "duplicate" in error_msg:
            logger.info("文件 %s 已存在于 Garmin Connect 中（重复上传，跳过）", filename)
            return True

        logger.error("上传 %s 失败: %s", filename, e)
        return False

    except Exception as e:
        logger.error("上传 %s 时发生意外错误: %s", filename, e)
        return False

    finally:
        if temp_path.exists():
            temp_path.unlink()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    """主同步流程。"""
    logger.info("=" * 60)
    logger.info("Wahoo -> Garmin Connect 国区 自动同步")
    logger.info(f"配置: SYNC_DAYS=%d, MAX_EMAILS=%d", SYNC_DAYS, MAX_EMAILS)
    logger.info("=" * 60)

    # 检查必要环境变量
    missing_vars = []
    if not IMAP_SERVER:
        missing_vars.append("IMAP_SERVER")
    if not IMAP_USER:
        missing_vars.append("IMAP_USER")
    if not IMAP_PASSWORD:
        missing_vars.append("IMAP_PASSWORD")
    if not WAHOO_EMAIL:
        missing_vars.append("WAHOO_EMAIL")
    if not WAHOO_PASSWORD:
        missing_vars.append("WAHOO_PASSWORD")
    if not GARMIN_EMAIL:
        missing_vars.append("GARMIN_EMAIL")
    if not GARMIN_PASSWORD:
        missing_vars.append("GARMIN_PASSWORD")

    if missing_vars:
        logger.error("缺少必要的环境变量: %s", ", ".join(missing_vars))
        logger.error("请在 GitHub Secrets 中配置这些变量")
        return 1

    # 加载已处理记录
    load_processed()

    # Step 1: 连接 IMAP，搜索邮件
    mail = None
    wahoo_emails: list[tuple[str, bytes]] = []
    try:
        mail = connect_imap()
        wahoo_emails = search_wahoo_emails(mail)
    except Exception as e:
        logger.error("搜索邮件失败: %s", e)
        return 1
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass
            logger.info("IMAP 连接已关闭")

    if not wahoo_emails:
        logger.info("没有找到 Wahoo 活动邮件")
        return 0

    # Step 2: 从每封邮件提取下载链接
    all_download_urls: list[str] = []
    for eid, raw_email in wahoo_emails:
        urls = extract_download_urls(raw_email)
        all_download_urls.extend(urls)

    # 用 workout_id 去重
    new_urls: list[str] = []
    for url in all_download_urls:
        wid = url_to_workout_id(url)
        if wid not in PROCESSED_WORKOUT_IDS:
            new_urls.append(url)

    logger.info("共找到 %d 个下载链接，其中 %d 个是新活动", len(all_download_urls), len(new_urls))

    if not new_urls:
        logger.info("没有新的活动需要同步")
        return 0

    # Step 3: 登录 Wahoo
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })

    try:
        if not login_wahoo(session):
            logger.error("Wahoo 登录失败，无法继续")
            return 1
    except Exception as e:
        logger.error("Wahoo 登录异常: %s", e)
        return 1

    # Step 4: 逐个访问链接，下载 .fit 文件
    downloaded_files: list[tuple[str, bytes, str]] = []

    for url in new_urls:
        workout_id = url_to_workout_id(url)
        result = download_fit_from_url(session, url)
        if result:
            filename, fit_data = result
            downloaded_files.append((filename, fit_data, workout_id))
        else:
            logger.warning("链接 %s 的 .fit 文件下载失败", url)

    logger.info("成功下载 %d/%d 个 .fit 文件", len(downloaded_files), len(new_urls))

    if not downloaded_files:
        logger.error("没有成功下载任何 .fit 文件")
        save_processed()
        return 1

    # Step 5: 登录 Garmin
    garmin_client = None
    try:
        garmin_client = login_garmin_cn()
    except Exception as e:
        logger.error("登录 Garmin 失败，无法继续上传: %s", e)
        save_processed()
        return 1

    # Step 6: 逐个上传
    success_count = 0
    fail_count = 0

    for filename, fit_data, workout_id in downloaded_files:
        success = upload_fit_to_garmin(garmin_client, filename, fit_data)
        if success:
            success_count += 1
            PROCESSED_WORKOUT_IDS.add(workout_id)
        else:
            fail_count += 1
            logger.warning("活动 %s 上传失败，将在下次运行时重试", workout_id)

    # Step 7: 保存记录
    save_processed()

    logger.info("=" * 60)
    logger.info("同步完成！成功: %d, 失败: %d, 总计: %d", success_count, fail_count, len(downloaded_files))
    logger.info("=" * 60)

    return 1 if fail_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
