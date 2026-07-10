#!/usr/bin/env python3
"""
Wahoo Fitness .fit 文件自动同步到 Garmin Connect 国区账户。

工作原理：
  1. 通过 IMAP 连接邮箱，搜索 Wahoo 发来的活动通知邮件
  2. 从邮件正文中提取 workout_summaries URL（含活动 ID）
  3. 通过 HTTP 模拟登录 wahooligan.com（SAML SSO，无 MFA）
  4. 访问活动页面，找到并下载 .fit 文件
  5. 登录 Garmin Connect 国区 (connect.garmin.cn)
  6. 将 .fit 文件上传到佳明国区账户
  7. 记录已处理的活动 ID，避免重复上传

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

SYNC_DAYS = int(os.getenv("SYNC_DAYS", "7"))
MAX_EMAILS = int(os.getenv("MAX_EMAILS", "100"))

WAHOO_SENDER = os.getenv("WAHOO_SENDER") or "wahooligan.com"
MAIL_FOLDER = os.getenv("MAIL_FOLDER") or "INBOX"

WORK_DIR = Path(os.getenv("GITHUB_WORKSPACE", ".")) / "data"
FIT_DIR = WORK_DIR / "fit_files"
PROCESSED_FILE = WORK_DIR / "processed_workouts.json"

PROCESSED_WORKOUT_IDS: set[str] = set()

WAHOO_BASE_URL = "https://wahooligan.com"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def load_processed():
    """加载已处理的活动 ID。"""
    global PROCESSED_WORKOUT_IDS
    if PROCESSED_FILE.exists():
        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                PROCESSED_WORKOUT_IDS = set(data.get("processed_ids", []))
            logger.info("已加载 %d 条已处理活动记录", len(PROCESSED_WORKOUT_IDS))
        except Exception as e:
            logger.warning("加载已处理记录失败: %s", e)
            PROCESSED_WORKOUT_IDS = set()
    else:
        logger.info("未找到已处理记录文件，从头开始处理")


def save_processed():
    """保存已处理的活动 ID。"""
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"processed_ids": list(PROCESSED_WORKOUT_IDS)},
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info("已保存 %d 条已处理活动记录", len(PROCESSED_WORKOUT_IDS))


def decode_mime_header(value: str) -> str:
    """解码 MIME 编码的邮件头。"""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


# ---------------------------------------------------------------------------
# IMAP 邮件处理
# ---------------------------------------------------------------------------
def connect_imap() -> imaplib.IMAP4_SSL:
    """连接 IMAP 服务器并登录。"""
    if not IMAP_SERVER or not IMAP_USER or not IMAP_PASSWORD:
        raise ValueError("IMAP_SERVER, IMAP_USER, IMAP_PASSWORD 环境变量未配置")

    logger.info("正在连接 IMAP 服务器 %s:%d ...", IMAP_SERVER, IMAP_PORT)
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(IMAP_USER, IMAP_PASSWORD)
    logger.info("IMAP 登录成功")

    mail.select(MAIL_FOLDER)
    return mail


def _is_recent_wahoo_email(raw_header: bytes) -> tuple[bool, str, str]:
    """检查邮件头是否属于最近的有效 Wahoo 邮件。"""
    msg = email.message_from_bytes(raw_header)

    from_addr = msg.get("From", "").lower()
    mail_date = msg.get("Date", "")
    msg_id = msg.get("Message-ID", "")

    if "wahoo" not in from_addr:
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
    """搜索 Wahoo 发来的新邮件，返回 (邮件ID, 邮件原始数据) 列表。"""
    logger.info("正在获取 Wahoo 邮件列表...")

    # QQ 邮箱只支持复合括号语法 (FROM "x")
    status, data = mail.search(None, f'(FROM "{WAHOO_SENDER}")')
    if status != "OK":
        raise RuntimeError(f"IMAP 搜索失败: {status}")

    email_ids = data[0].split()
    total_count = len(email_ids)

    if total_count == 0:
        logger.warning("FROM 搜索 '%s' 返回 0 封，退回到 ALL 搜索 + 本地过滤...", WAHOO_SENDER)
        status, data = mail.search(None, "ALL")
        if status != "OK":
            raise RuntimeError(f"IMAP 搜索失败: {status}")
        email_ids = data[0].split()
        total_count = len(email_ids)
        if not email_ids:
            logger.info("邮箱中没有邮件")
            return []
        logger.info("邮箱共有 %d 封邮件", total_count)
    else:
        logger.info("FROM 搜索返回 %d 封来自 %s 的邮件", total_count, WAHOO_SENDER)

    if len(email_ids) > MAX_EMAILS:
        email_ids = email_ids[-MAX_EMAILS:]
        logger.info("扫描最新的 %d 封（共 %d 封）", MAX_EMAILS, total_count)
    else:
        logger.info("扫描全部 %d 封", len(email_ids))

    # 逐个 fetch 精简头，过滤 FROM + 日期
    new_email_ids: list[bytes] = []
    checked_count = 0

    for eid in reversed(email_ids):
        checked_count += 1
        status, header_data = mail.fetch(
            eid, "(BODY.PEEK[HEADER.FIELDS (FROM DATE MESSAGE-ID)])"
        )
        if status != "OK":
            continue

        raw_header = header_data[0][1]
        is_valid, mail_date, msg_id = _is_recent_wahoo_email(raw_header)

        if not is_valid:
            continue

        new_email_ids.append(eid)
        logger.info("发现 Wahoo 邮件: ID=%s, Date=%s", eid.decode(), mail_date[:30])

    logger.info(
        "检查了 %d 封邮件，发现 %d 封 Wahoo 邮件", checked_count, len(new_email_ids)
    )

    if not new_email_ids:
        return []

    # fetch 完整内容
    results = []
    for eid in new_email_ids:
        status, msg_data = mail.fetch(eid, "(RFC822)")
        if status != "OK":
            logger.warning("获取邮件 %s 完整内容失败", eid)
            continue
        results.append((eid.decode(), msg_data[0][1]))

    return results


def extract_workout_urls(raw_email: bytes) -> list[str]:
    """从邮件正文中提取 workout_summaries URL。

    Wahoo 邮件中的链接格式如：
    https://wahooligan.com/workout_summaries/422997785
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

    # 正则匹配 workout_summaries URL
    pattern = r"https?://[^/\s]*wahooligan\.com/workout_summaries/(\d+)"
    matches = re.findall(pattern, full_body)

    # 去重，保持顺序
    seen = set()
    workout_ids = []
    for match in matches:
        if match not in seen:
            seen.add(match)
            workout_ids.append(match)

    if workout_ids:
        logger.info("从邮件中提取到 %d 个活动 ID: %s", len(workout_ids), workout_ids)
    else:
        logger.warning("邮件中未找到 workout_summaries 链接")

    return workout_ids


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
    # 成功后通常会重定向到 dashboard 或首页
    if resp.status_code == 200:
        # 检查页面内容是否包含登录失败标志
        resp_lower = resp.text.lower()
        if "invalid email or password" in resp_lower or "log in" in resp_lower and "password" in resp_lower:
            # 可能仍在登录页
            if "invalid" in resp_lower:
                logger.error("Wahoo 登录失败：邮箱或密码错误")
                return False
            logger.warning("登录后似乎仍在登录页，但未检测到明确错误，继续尝试...")

    logger.info("Wahoo 登录成功（HTTP %d，URL: %s）", resp.status_code, resp.url)
    return True


def download_fit_file(session: requests.Session, workout_id: str) -> tuple[str, bytes] | None:
    """下载指定活动的 .fit 文件。

    策略：
    1. 访问 workout_summaries/{id} 页面，解析 HTML 寻找 .fit 下载链接
    2. 如果找不到，尝试常见 URL 模式
    """
    summary_url = f"{WAHOO_BASE_URL}/workout_summaries/{workout_id}"
    logger.info("正在访问活动页面: %s", summary_url)

    resp = session.get(summary_url, timeout=30)
    if resp.status_code != 200:
        logger.warning("访问活动页面失败: HTTP %d", resp.status_code)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # 策略 1: 查找包含 .fit 的链接
    fit_link = None
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if ".fit" in href.lower():
            fit_link = href
            logger.info("在页面中找到 .fit 链接: %s", href)
            break

    # 策略 2: 查找 download/export 相关链接
    if not fit_link:
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].lower()
            text = a_tag.get_text(strip=True).lower()
            if "download" in href or "download" in text or "export" in href or "export" in text:
                fit_link = a_tag["href"]
                logger.info("找到下载链接: %s (文本: %s)", fit_link, text)
                break

    # 策略 3: 查找按钮或 data 属性中的下载 URL
    if not fit_link:
        for btn in soup.find_all(["button", "a"], attrs=True):
            for attr_name, attr_val in btn.attrs.items():
                if isinstance(attr_val, str) and ".fit" in attr_val.lower():
                    fit_link = attr_val
                    logger.info("在属性 %s 中找到 .fit 链接: %s", attr_name, fit_link)
                    break
            if fit_link:
                break

    # 策略 4: 在 JavaScript 代码中查找 .fit URL
    if not fit_link:
        for script in soup.find_all("script"):
            script_text = script.string or ""
            fit_match = re.search(r'["\']([^"\']*\.fit[^"\']*)["\']', script_text, re.IGNORECASE)
            if fit_match:
                fit_link = fit_match.group(1)
                logger.info("在 JS 中找到 .fit 链接: %s", fit_link)
                break

    # 策略 5: 尝试常见 URL 模式
    if not fit_link:
        candidate_urls = [
            f"{WAHOO_BASE_URL}/workout_summaries/{workout_id}.fit",
            f"{WAHOO_BASE_URL}/workouts/{workout_id}.fit",
            f"{WAHOO_BASE_URL}/workout_summaries/{workout_id}/download",
            f"{WAHOO_BASE_URL}/workout_summaries/{workout_id}/download.fit",
            f"{WAHOO_BASE_URL}/workouts/{workout_id}/download",
            f"{WAHOO_BASE_URL}/workouts/{workout_id}/export.fit",
        ]
        for url in candidate_urls:
            try:
                head_resp = session.head(url, timeout=15, allow_redirects=True)
                content_type = head_resp.headers.get("Content-Type", "")
                if head_resp.status_code == 200 and (
                    "application/octet-stream" in content_type
                    or "fit" in content_type.lower()
                    or "application/vnd.garmin" in content_type.lower()
                ):
                    fit_link = url
                    logger.info("HEAD 探测成功: %s (Content-Type: %s)", url, content_type)
                    break
            except Exception:
                continue

    if not fit_link:
        logger.error("活动 %s 页面中未找到 .fit 下载链接", workout_id)
        # 保存页面源码用于调试
        debug_file = FIT_DIR / f"debug_{workout_id}.html"
        FIT_DIR.mkdir(parents=True, exist_ok=True)
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(resp.text)
        logger.info("页面源码已保存到 %s 供调试", debug_file)
        return None

    # 构造完整 URL
    if fit_link.startswith("/"):
        fit_link = f"{WAHOO_BASE_URL}{fit_link}"
    elif not fit_link.startswith("http"):
        fit_link = f"{WAHOO_BASE_URL}/{fit_link}"

    # 下载 .fit 文件
    logger.info("正在下载 .fit 文件: %s", fit_link)
    resp = session.get(fit_link, timeout=60)
    if resp.status_code != 200:
        logger.error("下载 .fit 失败: HTTP %d", resp.status_code)
        return None

    fit_data = resp.content
    if len(fit_data) < 100:
        logger.error("下载的 .fit 文件过小 (%d bytes)，可能不是有效文件", len(fit_data))
        return None

    # 从 URL 或 header 提取文件名
    filename = f"wahoo_{workout_id}.fit"
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

    # Step 1: 连接 IMAP，搜索邮件，提取活动 URL
    mail = None
    all_workout_ids: list[str] = []
    try:
        mail = connect_imap()
        emails = search_wahoo_emails(mail)

        for eid, raw_email in emails:
            workout_ids = extract_workout_urls(raw_email)
            all_workout_ids.extend(workout_ids)

    except Exception as e:
        logger.error("搜索/提取邮件失败: %s", e)
        return 1
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass
            logger.info("IMAP 连接已关闭")

    if not all_workout_ids:
        logger.info("没有找到 Wahoo 活动邮件")
        return 0

    # 过滤已处理的活动
    new_workout_ids = [wid for wid in all_workout_ids if wid not in PROCESSED_WORKOUT_IDS]
    logger.info("共找到 %d 个活动，其中 %d 个是新活动", len(all_workout_ids), len(new_workout_ids))

    if not new_workout_ids:
        logger.info("没有新的活动需要同步")
        return 0

    # Step 2: 登录 Wahoo
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

    # Step 3: 下载每个活动的 .fit 文件
    downloaded_files: list[tuple[str, bytes, str]] = []  # (filename, data, workout_id)

    for workout_id in new_workout_ids:
        result = download_fit_file(session, workout_id)
        if result:
            filename, fit_data = result
            downloaded_files.append((filename, fit_data, workout_id))
        else:
            logger.warning("活动 %s 的 .fit 文件下载失败", workout_id)

    logger.info("成功下载 %d/%d 个 .fit 文件", len(downloaded_files), len(new_workout_ids))

    if not downloaded_files:
        logger.error("没有成功下载任何 .fit 文件")
        save_processed()
        return 1

    # Step 4: 登录 Garmin
    garmin_client = None
    try:
        garmin_client = login_garmin_cn()
    except Exception as e:
        logger.error("登录 Garmin 失败，无法继续上传: %s", e)
        save_processed()
        return 1

    # Step 5: 逐个上传
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

    # Step 6: 保存记录
    save_processed()

    logger.info("=" * 60)
    logger.info("同步完成！成功: %d, 失败: %d, 总计: %d", success_count, fail_count, len(downloaded_files))
    logger.info("=" * 60)

    return 1 if fail_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
