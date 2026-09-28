import logging
import os
from threading import RLock
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
_job_specs = {}  # job_id -> (user_id, cron_expr)，用于避免重复替换未变化的任务
_job_lock = RLock()
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

        # APScheduler 在线程中执行，不会自动继承 Flask 上下文。
        # 查寝过程可能发送邮件，因此必须在这个上下文内调用任务。
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


def _cron_sort_key(cron_expr):
    """按小时和分钟排序 cron，避免表单顺序变化导致任务 ID 变化。"""
    fields = cron_expr.split()
    try:
        return int(fields[1]), int(fields[0]), cron_expr
    except (IndexError, ValueError):
        return 99, 99, cron_expr


def _normalized_cron_times(user):
    """规范化用户的 cron 列表：去空白、去重并按时间排序。"""
    unique = {
        ' '.join(str(expr).split())
        for expr in user.get_cron_times()
        if str(expr).strip()
    }
    return sorted(unique, key=_cron_sort_key)


def _sync_user_jobs(user):
    """按用户配置增量同步任务，返回新增、修改、保持不变的数量。"""
    desired = {}
    for idx, cron_expr in enumerate(_normalized_cron_times(user)):
        job_id = f'gotobed_{user.id}_{idx}'
        try:
            desired[job_id] = (
                (user.id, cron_expr),
                CronTrigger.from_crontab(cron_expr, timezone='Asia/Shanghai'),
            )
        except Exception as exc:
            logger.error('添加调度失败: 用户 %s, cron=%s, 错误: %s', user.username, cron_expr, exc)

    job_ids = []
    added = changed = unchanged = 0
    with _job_lock:
        prefix = f'gotobed_{user.id}_'
        previous_ids = set(_jobs.get(user.id, []))
        previous_ids.update(
            job.id for job in scheduler.get_jobs() if job.id.startswith(prefix)
        )
        for job_id, (spec, trigger) in desired.items():
            current_job = scheduler.get_job(job_id)
            current_spec = _job_specs.get(job_id)
            if current_job is None:
                scheduler.add_job(
                    _execute_gotobed,
                    trigger=trigger,
                    args=[user.id],
                    id=job_id,
                    max_instances=1,
                )
                added += 1
                logger.info('已添加调度: 用户 %s, cron=%s', user.username, spec[1])
            elif current_spec != spec:
                scheduler.reschedule_job(job_id, trigger=trigger)
                changed += 1
                logger.info('已更新调度: 用户 %s, cron=%s', user.username, spec[1])
            else:
                unchanged += 1
            _job_specs[job_id] = spec
            job_ids.append(job_id)

        # 用户减少时间或删除账号时，清理该用户不再需要的任务。
        for job_id in previous_ids - set(job_ids):
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass
            _job_specs.pop(job_id, None)
            logger.info('已移除失效调度: %s', job_id)

        _jobs[user.id] = job_ids
    return added, changed, unchanged


def add_user_job(user):
    """按用户配置增量添加或更新调度任务（支持多个时间）。"""
    if not _scheduler_enabled:
        logger.debug('调度器未启用，跳过用户任务更新: user_id=%s', user.id)
        return
    _sync_user_jobs(user)


def remove_user_job(user_id: int):
    """移除用户的所有调度任务"""
    if not _scheduler_enabled:
        return
    with _job_lock:
        prefix = f'gotobed_{user_id}_'
        job_ids = set(_jobs.pop(user_id, []))
        job_ids.update(
            job.id for job in scheduler.get_jobs() if job.id.startswith(prefix)
        )
        for job_id in job_ids:
            try:
                scheduler.remove_job(job_id)
                logger.info('已移除调度: %s', job_id)
            except Exception:
                pass
            _job_specs.pop(job_id, None)


def update_user_job(user):
    """按用户最新配置增量更新调度任务。"""
    if not _scheduler_enabled:
        return
    if user.enabled:
        _sync_user_jobs(user)
    else:
        remove_user_job(user.id)


def sync_jobs():
    """从 SQLite 重新加载任务，使 Web 端的账号变更生效。"""
    if not _scheduler_enabled or _app is None:
        return

    from .models import User

    with _app.app_context():
        enabled_users = User.query.filter_by(enabled=True).all()
        desired_ids = set()
        added = changed = unchanged = 0
        for user in enabled_users:
            user_added, user_changed, user_unchanged = _sync_user_jobs(user)
            added += user_added
            changed += user_changed
            unchanged += user_unchanged
            desired_ids.update(_jobs.get(user.id, []))

        with _job_lock:
            stale_jobs = [
                job for job in scheduler.get_jobs()
                if job.id.startswith('gotobed_') and job.id not in desired_ids
            ]
            for job in stale_jobs:
                scheduler.remove_job(job.id)
                _job_specs.pop(job.id, None)
                logger.info('已移除失效调度: %s', job.id)
                _jobs.pop(_user_id_from_job_id(job.id), None)

        logger.info(
            '调度同步完成：新增 %s 个，修改 %s 个，删除 %s 个，保持不变 %s 个',
            added, changed, len(stale_jobs), unchanged,
        )


def _user_id_from_job_id(job_id):
    """从任务 ID 中解析用户 ID，仅用于清理缓存。"""
    try:
        return int(job_id.removeprefix('gotobed_').rsplit('_', 1)[0])
    except (TypeError, ValueError):
        return None


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
