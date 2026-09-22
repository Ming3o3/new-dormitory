import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# 同一时间触发的任务进入队列等待，不因默认的一秒宽限期而丢失。
try:
    _scheduler_workers = max(1, int(os.environ.get('SCHEDULER_MAX_WORKERS', '20')))
except ValueError:
    _scheduler_workers = 20

scheduler = BackgroundScheduler(
    timezone='Asia/Shanghai',
    executors={'default': ThreadPoolExecutor(max_workers=_scheduler_workers)},
    job_defaults={
        'coalesce': False,
        'misfire_grace_time': None,
    },
)
_jobs = {}  # user_id -> [job_id1, job_id2, ...]  一个用户可有多个定时任务
_app = None  # 保存 app 引用
_scheduler_enabled = False

BJT = ZoneInfo('Asia/Shanghai')


def _execute_gotobed(user_id: int):
    """调度任务回调：执行指定用户的查寝"""
    from .models import db, User, Log
    from .tasks.gotobed import run_gotobed
    from .crypto import decrypt_password

    if _app is None:
        logger.error('Flask app 未初始化')
        return

    # 保持 SQLite 事务简短。下面的网络和 SMTP 操作可能耗时几十秒，
    # 期间不能一直占用数据库连接。
    with _app.app_context():
        user = db.session.get(User, user_id)
        if not user or not user.enabled:
            logger.warning(f'用户 {user_id} 不存在或已禁用，跳过')
            return
        task_input = {
            'username': user.username,
            'password': decrypt_password(user.password_encrypted),
            'principal': user.principal,
            'credential': user.credential,
            'email': user.email,
            'campus': user.campus,
        }
        db.session.remove()

    result = run_gotobed(**task_input)

    with _app.app_context():
        log = Log(
            user_id=user_id,
            status=result['status'],
            message=result['message'],
        )
        db.session.add(log)
        db.session.commit()
        logger.info(f'用户 {task_input["username"]} 查寝完成: {result["status"]}')


def _cleanup_old_logs():
    """清理 5 天前的执行日志"""
    from .models import db, Log

    if _app is None:
        logger.error('Flask app 未初始化')
        return

    with _app.app_context():
        cutoff = datetime.now(BJT).replace(tzinfo=None) - timedelta(days=5)
        count = Log.query.filter(Log.executed_at < cutoff).delete()
        db.session.commit()
        if count > 0:
            logger.info(f'已清理 {count} 条过期日志（{cutoff.strftime("%Y-%m-%d %H:%M")} 之前）')


def add_user_job(user):
    """为用户添加调度任务（支持多个时间）"""
    if not _scheduler_enabled:
        logger.debug('调度器未启用，跳过用户任务更新: user_id=%s', user.id)
        return
    job_ids = []
    for idx, cron_expr in enumerate(user.get_cron_times()):
        job_id = f'gotobed_{user.id}_{idx}'
        try:
            trigger = CronTrigger.from_crontab(cron_expr, timezone='Asia/Shanghai')
            scheduler.add_job(
                _execute_gotobed,
                trigger=trigger,
                args=[user.id],
                id=job_id,
                replace_existing=True,
                max_instances=1,
            )
            job_ids.append(job_id)
            logger.info(f'已添加调度: 用户 {user.username}, cron={cron_expr}')
        except Exception as e:
            logger.error(f'添加调度失败: 用户 {user.username}, cron={cron_expr}, 错误: {e}')
    _jobs[user.id] = job_ids


def remove_user_job(user_id: int):
    """移除用户的所有调度任务"""
    if not _scheduler_enabled:
        return
    job_ids = _jobs.pop(user_id, [])
    for job_id in job_ids:
        try:
            scheduler.remove_job(job_id)
            logger.info(f'已移除调度: {job_id}')
        except Exception:
            pass


def update_user_job(user):
    """更新用户的调度任务（删除旧的，添加新的）"""
    remove_user_job(user.id)
    if user.enabled:
        add_user_job(user)


def sync_jobs():
    """从 SQLite 重新加载任务，使 Web 端的账号变更生效。"""
    if not _scheduler_enabled or _app is None:
        return

    from .models import User

    with _app.app_context():
        enabled_users = User.query.filter_by(enabled=True).all()
        desired_ids = set()
        for user in enabled_users:
            desired_ids.update(
                f'gotobed_{user.id}_{idx}'
                for idx, _cron_expr in enumerate(user.get_cron_times())
            )
            add_user_job(user)

        for job in scheduler.get_jobs():
            if job.id.startswith('gotobed_') and job.id not in desired_ids:
                scheduler.remove_job(job.id)
                logger.info('已移除失效调度: %s', job.id)


def init_scheduler(app):
    """初始化调度器，加载所有启用的用户"""
    global _app, _scheduler_enabled
    _app = app
    _scheduler_enabled = True

    with app.app_context():
        from .models import User
        users = User.query.filter_by(enabled=True).all()
        for user in users:
            add_user_job(user)

    # 每天凌晨 4 点清理 5 天前的日志
    scheduler.add_job(
        _cleanup_old_logs,
        trigger=CronTrigger(hour=4, minute=0, timezone='Asia/Shanghai'),
        id='cleanup_old_logs',
        replace_existing=True,
    )

    scheduler.add_job(
        sync_jobs,
        trigger=IntervalTrigger(seconds=30, timezone='Asia/Shanghai'),
        id='sync_jobs',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info(f'调度器已启动，共加载 {len(users)} 个用户任务')
