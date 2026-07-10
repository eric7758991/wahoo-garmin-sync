#!/usr/bin/env python3
"""
Wahoo Fitness .fit 文件自动同步到 Garmin Connect 国区账户。

工作原理：
  1. 通过 IMAP 连接到邮箱，搜索 Wahoo 发来的活动邮件
  2. 从邮件中提取 .fit 附件并下载
  3. 登录 Garmin Connect 国区 (connect.garmin.cn)
  4. 将 .fit 文件上传到佳明国区账户
  5. 记录已处理的邮件，避免重复上传

触发方式：
  - GitHub Actions 定时调度（schedule cron）
  - 手动触发（workflow_dispatch）

环境变量（通过 GitHub Secrets 配置）：
  IMAP_SERVER      邮箱 IMAP 服务器地址（如 imap.gmail.com）
  IMAP_PORT        IMAP 端口（通常 993）
  IMAP_USER        邮箱地址
  IMAP_PASSWORD    邮箱密码或应用专用密码
  GARMIN_EMAIL     佳明国区账户邮箱
  GARMIN_PASSWORD  佳明国区账户密码
  WAHOO_SENDER     Wahoo 发件人地址（可选，默认 wahoofitness.com）
  MAIL_FOLDER       邮箱中搜索的文件夹（可选，默认 INBOX）
  SYNC_DAYS        只处理最近 N 天内的邮件（可选，默认 1）
  MAX_EMAILS       单次运行最多检查多少封邮件（可选，默认 20）
"""

import hashlib
import imaplib
import logging
import os
import sys
import email
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

# 第三方依赖
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
IMAP_PORT = 993  # IMAP over SSL 固定端口，无需配置
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")

GARMIN_EMAIL = os.getenv("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD", "")

# 只搜索最近 N 天的邮件
SYNC_DAYS = int(os.getenv("SYNC_DAYS", "1"))

# 单次运行最多检查多少封邮件（最新的 N 封），防止超时
MAX_EMAILS = int(os.getenv("MAX_EMAILS", "20"))

# Wahoo 邮件发件人 —— 可根据实际邮件调整
WAHOO_SENDER = os.getenv("WAHOO_SENDER") or "wahoofitness.com"

# 邮箱文件夹
MAIL_FOLDER = os.getenv("MAIL_FOLDER") or "INBOX"

# 本地工作目录
WORK_DIR = Path(os.getenv("GITHUB_WORKSPACE", ".")) / "data"
FIT_DIR = WORK_DIR / "fit_files"
PROCESSED_FILE = WORK_DIR / "processed_emails.json"

# 已处理邮件 ID 缓存 —— 用于去重
PROCESSED_EMAIL_IDS: set[str] = set()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def load_processed_emails():
    """从本地缓存文件加载已处理的邮件 ID。"""
    global PROCESSED_EMAIL_IDS
    if PROCESSED_FILE.exists():
        import json

        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                PROCESSED_EMAIL_IDS = set(data.get("processed_ids", []))
            logger.info("已加载 %d 条已处理邮件记录", len(PROCESSED_EMAIL_IDS))
        except Exception as e:
            logger.warning("加载已处理记录失败，将重新开始: %s", e)
            PROCESSED_EMAIL_IDS = set()
    else:
        logger.info("未找到已处理记录文件，将从头开始处理")


def save_processed_emails():
    """将已处理的邮件 ID 保存到本地缓存文件。"""
    import json

    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"processed_ids": list(PROCESSED_EMAIL_IDS)},
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info("已保存 %d 条已处理邮件记录", len(PROCESSED_EMAIL_IDS))


def get_email_fingerprint(msg_id: str, mail_date: str = "") -> str:
    """生成邮件唯一指纹（用于去重）。

    使用邮件 ID + 日期生成 hash，避免不同邮件因 ID 重复而误判。
    """
    raw = f"{msg_id}|{mail_date}"
    return hashlib.md5(raw.encode()).hexdigest()


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
    """检查邮件头是否属于最近的有效 Wahoo 邮件。

    Args:
        raw_header: 邮件头的原始 bytes

    Returns:
        (是否有效, 日期字符串, 邮件ID)
    """
    msg = email.message_from_bytes(raw_header)

    from_addr = msg.get("From", "").lower()
    mail_date = msg.get("Date", "")
    msg_id = msg.get("Message-ID", "")

    # FROM 过滤
    if WAHOO_SENDER.lower() not in from_addr:
        return False, mail_date, msg_id

    # 日期过滤：只处理 SYNC_DAYS 天内的
    if mail_date:
        try:
            dt = parsedate_to_datetime(mail_date)
            cutoff = datetime.now(UTC) - timedelta(days=SYNC_DAYS)
            if dt < cutoff:
                return False, mail_date, msg_id
        except Exception:
            pass  # 日期解析失败就保留

    return True, mail_date, msg_id


def search_wahoo_emails(mail: imaplib.IMAP4_SSL) -> list[tuple[str, bytes]]:
    """搜索 Wahoo 发来的新邮件，返回 (邮件ID, 邮件原始数据) 列表。

    优化策略（应对 QQ 邮箱等 IMAP 实现不标准的问题）：
    1. 用 search(ALL) 获取全部邮件 ID（兼容所有 IMAP 服务器）
    2. 只取最新的 MAX_EMAILS 封（防止超时）
    3. 每封只 fetch 精简头（几百字节），本地过滤 FROM + 日期 + 去重
    4. 只对有效的新邮件 fetch 完整内容（RFC822，含附件）
    """
    logger.info("正在获取邮箱中的邮件列表...")

    # 步骤 1：获取所有邮件 ID（只有 ID，不 fetch 内容，非常快）
    status, data = mail.search(None, "ALL")
    if status != "OK":
        raise RuntimeError(f"IMAP 搜索失败: {status}")

    all_ids = data[0].split()
    logger.info("邮箱共有 %d 封邮件，将检查最新的 %d 封", len(all_ids), MAX_EMAILS)

    if not all_ids:
        return []

    # 步骤 2：只取最新的 MAX_EMAILS 封（邮件 ID 递增，新邮件 ID 更大）
    recent_ids = all_ids[-MAX_EMAILS:]

    # 步骤 3：每封只 fetch 精简头（几百字节）
    new_email_ids: list[tuple[bytes, str, str]] = []  # (eid, date, msg_id)
    checked_count = 0
    for eid in reversed(recent_ids):  # 从新到旧检查
        checked_count += 1

        status, header_data = mail.fetch(eid, "(BODY.PEEK[HEADER.FIELDS (FROM DATE MESSAGE-ID)])")
        if status != "OK":
            logger.warning("获取邮件 %s 头失败", eid)
            continue

        raw_header = header_data[0][1]
        is_valid, mail_date, msg_id = _is_recent_wahoo_email(raw_header)

        if not is_valid:
            continue

        # 去重检查
        fingerprint = get_email_fingerprint(eid.decode(), mail_date)
        if fingerprint in PROCESSED_EMAIL_IDS:
            continue

        new_email_ids.append((eid, mail_date, msg_id))
        logger.info("发现新 Wahoo 邮件: ID=%s, Date=%s, MsgID=%s...",
                    eid.decode(), mail_date[:30], msg_id[:30] if msg_id else "N/A")

    logger.info("检查了 %d 封邮件，发现 %d 封新的 Wahoo 邮件", checked_count, len(new_email_ids))

    if not new_email_ids:
        return []

    # 步骤 4：只对新邮件 fetch 完整内容（含附件）
    results = []
    for eid, mail_date, msg_id in new_email_ids:
        status, msg_data = mail.fetch(eid, "(RFC822)")
        if status != "OK":
            logger.warning("获取邮件 %s 完整内容失败", eid)
            continue
        results.append((eid.decode(), msg_data[0][1]))

    return results


def extract_fit_attachments(raw_email: bytes) -> list[tuple[str, bytes]]:
    """从邮件中提取 .fit 附件。

    返回 [(文件名, 文件内容), ...]
    """
    msg = email.message_from_bytes(raw_email)

    subject = decode_mime_header(msg.get("Subject", ""))
    date_str = msg.get("Date", "")
    from_addr = msg.get("From", "")

    logger.info("解析邮件: Subject='%s', Date='%s', From='%s'", subject, date_str, from_addr)

    fit_files = []
    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))
        filename = part.get_filename()

        if "attachment" not in content_disposition.lower():
            continue

        if not filename:
            continue

        filename = decode_mime_header(filename)

        if not filename.lower().endswith(".fit"):
            logger.debug("跳过非 .fit 附件: %s", filename)
            continue

        payload = part.get_payload(decode=True)
        if payload:
            fit_files.append((filename, payload))
            logger.info("找到 .fit 附件: %s (%d bytes)", filename, len(payload))

    if not fit_files:
        logger.warning("邮件中未找到 .fit 附件")

    return fit_files


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
    logger.info(f"配置: SYNC_DAYS={SYNC_DAYS}, MAX_EMAILS={MAX_EMAILS}, WAHOO_SENDER={WAHOO_SENDER}")
    logger.info("=" * 60)

    # 检查必要环境变量
    missing_vars = []
    if not IMAP_SERVER:
        missing_vars.append("IMAP_SERVER")
    if not IMAP_USER:
        missing_vars.append("IMAP_USER")
    if not IMAP_PASSWORD:
        missing_vars.append("IMAP_PASSWORD")
    if not GARMIN_EMAIL:
        missing_vars.append("GARMIN_EMAIL")
    if not GARMIN_PASSWORD:
        missing_vars.append("GARMIN_PASSWORD")

    if missing_vars:
        logger.error("缺少必要的环境变量: %s", ", ".join(missing_vars))
        logger.error("请在 GitHub Secrets 中配置这些变量")
        return 1

    # 加载已处理邮件记录
    load_processed_emails()

    # Step 1: 连接 IMAP
    mail = None
    try:
        mail = connect_imap()
    except Exception as e:
        logger.error("连接 IMAP 失败: %s", e)
        return 1

    # Step 2: 搜索并提取 .fit 附件
    new_fit_files: list[tuple[str, bytes, str]] = []  # (filename, data, fingerprint)
    try:
        emails = search_wahoo_emails(mail)

        for eid, raw_email in emails:
            msg = email.message_from_bytes(raw_email)
            mail_date = msg.get("Date", "")
            fingerprint = get_email_fingerprint(eid, mail_date)

            logger.info("处理新邮件: ID=%s", eid)

            attachments = extract_fit_attachments(raw_email)
            for filename, fit_data in attachments:
                new_fit_files.append((filename, fit_data, fingerprint))

            # 标记邮件为已处理（无论是否有 .fit 附件）
            PROCESSED_EMAIL_IDS.add(fingerprint)

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

    if not new_fit_files:
        logger.info("没有新的 .fit 文件需要上传")
        save_processed_emails()
        return 0

    logger.info("共找到 %d 个新的 .fit 文件待上传", len(new_fit_files))

    # Step 3: 登录 Garmin
    garmin_client = None
    try:
        garmin_client = login_garmin_cn()
    except Exception as e:
        logger.error("登录 Garmin 失败，无法继续上传: %s", e)
        save_processed_emails()
        return 1

    # Step 4: 逐个上传
    success_count = 0
    fail_count = 0

    for filename, fit_data, fingerprint in new_fit_files:
        success = upload_fit_to_garmin(garmin_client, filename, fit_data)
        if success:
            success_count += 1
        else:
            fail_count += 1
            # 上传失败的从已处理集合中移除，下次重试
            PROCESSED_EMAIL_IDS.discard(fingerprint)
            logger.warning("文件 %s 上传失败，将在下次运行时重试", filename)

    # Step 5: 保存记录
    save_processed_emails()

    logger.info("=" * 60)
    logger.info("同步完成！成功: %d, 失败: %d, 总计: %d", success_count, fail_count, len(new_fit_files))
    logger.info("=" * 60)

    return 1 if fail_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
