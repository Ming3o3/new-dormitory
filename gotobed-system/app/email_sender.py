import smtplib
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText

from flask import current_app

logger = logging.getLogger(__name__)

# 北京时间
BJT = ZoneInfo('Asia/Shanghai')

# 中文星期映射
_WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def get_beijing_time():
    now = datetime.now(BJT)
    weekday = _WEEKDAYS[now.weekday()]
    return now.strftime(f'%Y-%m-%d {weekday} %H:%M')


def _close_smtp(smtp):
    if smtp is None:
        return
    try:
        smtp.quit()
    except Exception:
        try:
            smtp.close()
        except Exception:
            pass


def _send_via_port(smtp_host, smtp_port, smtp_user, smtp_pass, msg, to_address, timeout=15):
    smtp = None
    stage = 'prepare'
    try:
        if int(smtp_port) == 465:
            stage = 'connect_ssl'
            logger.info('SMTP 阶段=%s, port=%s', stage, smtp_port)
            smtp = smtplib.SMTP_SSL(smtp_host, 465, timeout=timeout)
        else:
            stage = 'connect_plain'
            logger.info('SMTP 阶段=%s, port=%s', stage, smtp_port)
            smtp = smtplib.SMTP(smtp_host, int(smtp_port), timeout=timeout)
            stage = 'starttls'
            logger.info('SMTP 阶段=%s, port=%s', stage, smtp_port)
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
        stage = 'login'
        logger.info('SMTP 阶段=%s, port=%s', stage, smtp_port)
        smtp.login(smtp_user, smtp_pass)
        stage = 'sendmail'
        logger.info('SMTP 阶段=%s, port=%s', stage, smtp_port)
        smtp.sendmail(smtp_user, to_address, msg.as_string())
        return True, stage, None
    except Exception as e:
        return False, stage, e
    finally:
        _close_smtp(smtp)


def send_email(subject: str, content: str, to_address: str):
    """发送邮件，返回是否发送成功。"""
    if not to_address:
        logger.warning('邮件发送跳过: 收件地址为空')
        return False

    smtp_host = current_app.config.get('SMTP_HOST', '')
    smtp_port = current_app.config.get('SMTP_PORT', 465)
    smtp_user = current_app.config.get('SMTP_USER', '')
    smtp_pass = current_app.config.get('SMTP_PASS', '')
    smtp_timeout = current_app.config.get('SMTP_TIMEOUT', 15)

    if not smtp_user or not smtp_pass:
        logger.error(
            '邮件发送失败: SMTP 配置不完整 (host=%s, port=%s, user=%s, pass=%s)',
            smtp_host or '<empty>', smtp_port, smtp_user or '<empty>',
            'SET' if smtp_pass else 'EMPTY',
        )
        return False

    msg = MIMEText(content, 'plain', 'utf-8')
    msg['From'] = smtp_user
    msg['To'] = to_address
    msg['Subject'] = subject

    started = time.monotonic()
    primary_port = int(smtp_port)
    fallback_port = 465 if primary_port != 465 else 587
    ports = [primary_port, fallback_port]
    logger.info(
        '邮件发送开始: host=%s, ports=%s, user=%s, to=%s, subject=%s',
        smtp_host, ports, smtp_user, to_address, subject,
    )

    last_error = None
    for index, port in enumerate(ports):
        ok, stage, error = _send_via_port(
            smtp_host, port, smtp_user, smtp_pass, msg, to_address, smtp_timeout
        )
        if ok:
            logger.info('邮件发送成功: to=%s, port=%s, elapsed=%.2fs',
                        to_address, port, time.monotonic() - started)
            return True
        last_error = error
        if index == 0:
            logger.warning('邮件发送失败，尝试备用端口: port=%s, stage=%s, error=%s',
                           port, stage, error)
        else:
            logger.error('邮件发送失败: port=%s, stage=%s, error=%s, elapsed=%.2fs',
                         port, stage, error, time.monotonic() - started, exc_info=True)
    return False


def send_verification_code(to_address: str, code: str, purpose: str):
    """发送注册或找回密码验证码。"""
    action = {
        'register': '注册账号',
        'reset_password': '重置密码',
        'notify_email': '通知邮箱验证',
    }.get(purpose, '邮箱验证')
    subject = f'查寝管理系统 - {action}验证码'
    content = (
        f'您好，您正在进行{action}。\n\n'
        f'验证码：{code}\n'
        '验证码 10 分钟内有效，请勿将验证码告知他人。\n\n'
        '如非本人操作，请忽略此邮件。'
    )
    return send_email(subject, content, to_address)


def send_gotobed_result(content: str, to_address: str) -> bool:
    """发送查寝结果通知"""
    formatted_date = get_beijing_time()
    result_status = '✅成功' if '成功' in content else '❌失败'
    subject = f'查寝 {result_status} {formatted_date}'
    body = f'签到结果：{content}'
    return send_email(subject, body, to_address)
