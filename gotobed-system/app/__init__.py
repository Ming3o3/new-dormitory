import os
import logging
import time
from contextlib import contextmanager
from datetime import datetime
import fcntl
from flask import Flask, jsonify
from flask_login import LoginManager
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from dotenv import load_dotenv
except ImportError:  # 未安装可选依赖时仍允许通过系统环境变量启动
    def load_dotenv(*args, **kwargs):
        return False

from .models import db, Account, User, BJT

login_manager = LoginManager()
logger = logging.getLogger(__name__)


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _configure_sqlite(app):
    """为现有的多进程部署配置 SQLite。"""
    if not app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:'):
        return

    engine = db.engine

    @event.listens_for(engine, 'connect')
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute('PRAGMA busy_timeout=5000')
            cursor.execute('PRAGMA synchronous=NORMAL')
        finally:
            cursor.close()

    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL')
        connection.exec_driver_sql('PRAGMA busy_timeout=5000')
        connection.exec_driver_sql('PRAGMA synchronous=NORMAL')


@contextmanager
def _startup_lock(data_dir):
    """在多个 Worker 和容器之间串行执行数据库初始化。"""
    lock_path = os.path.join(data_dir, '.startup.lock')
    with open(lock_path, 'a+') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _register_health_routes(app):
    @app.get('/health/live')
    def health_live():
        return jsonify({'status': 'ok'})

    @app.get('/health/ready')
    def health_ready():
        started = time.monotonic()
        try:
            db.session.execute(text('SELECT 1'))
            latency_ms = round((time.monotonic() - started) * 1000, 2)
            return jsonify({
                'status': 'ok',
                'database': 'ok',
                'latency_ms': latency_ms,
            })
        except Exception as exc:
            db.session.rollback()
            logger.exception('健康检查失败')
            return jsonify({
                'status': 'degraded',
                'database': 'error',
                'error': type(exc).__name__,
            }), 503

    @app.get('/health')
    def health():
        return health_ready()


def create_app():
    # 支持本地直接运行时自动读取 gotobed-system/.env，Docker 仍可通过 env_file 注入。
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
    app = Flask(__name__)

    # 数据目录（使用项目根目录，确保 Docker volume 挂载生效）
    data_dir = os.path.join(os.path.dirname(app.root_path), 'data')
    os.makedirs(data_dir, exist_ok=True)

    # 加载配置
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
        'DATABASE_URL', 'sqlite:///' + os.path.join(data_dir, 'gotobed.db')
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {
            'timeout': float(os.environ.get('SQLITE_BUSY_TIMEOUT', '5')),
            'check_same_thread': False,
        },
    }
    app.config['ADMIN_PASSWORD'] = os.environ.get('ADMIN_PASSWORD', 'admin123')
    app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', os.environ.get('SMTP_USER', ''))
    app.config['FERNET_KEY'] = os.environ.get('FERNET_KEY', '')
    app.config['SMTP_HOST'] = os.environ.get('SMTP_HOST', 'smtp.qq.com')
    app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', '465'))
    app.config['SMTP_TIMEOUT'] = float(os.environ.get('SMTP_TIMEOUT', '15'))
    app.config['SMTP_USER'] = os.environ.get('SMTP_USER', '')
    app.config['SMTP_PASS'] = os.environ.get('SMTP_PASS', '')
    app.config['ENABLE_SCHEDULER'] = _env_bool('ENABLE_SCHEDULER', False)
    logger.info(
        'SMTP 配置已加载: host=%s, port=%s, user=%s, pass=%s',
        app.config['SMTP_HOST'], app.config['SMTP_PORT'],
        app.config['SMTP_USER'] or '<empty>',
        'SET' if app.config['SMTP_PASS'] else 'EMPTY',
    )

    # 初始化扩展
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'

    # 注册蓝图
    from .routes import register_blueprints
    register_blueprints(app)
    _register_health_routes(app)

    @app.teardown_appcontext
    def _cleanup_db_session(exception=None):
        if exception is not None:
            db.session.rollback()
        db.session.remove()

    # 创建数据库表
    with _startup_lock(data_dir):
        with app.app_context():
            _configure_sqlite(app)
            db.create_all()
            _upgrade_legacy_schema()
            _ensure_admin_account(app)

    # 调度器只在独立的单实例服务中运行，避免多个 Gunicorn Worker
    # 重复创建定时任务。
    if app.config['ENABLE_SCHEDULER']:
        from .scheduler import init_scheduler
        init_scheduler(app)
    else:
        logger.info('调度器未启用：当前进程仅提供 Web/API 服务')

    return app


def _upgrade_legacy_schema():
    """为已有 SQLite 数据库补充新字段，避免升级后旧数据无法启动。"""
    inspector = inspect(db.engine)
    if 'users' not in inspector.get_table_names():
        return
    columns = {column['name'] for column in inspector.get_columns('users')}
    if 'owner_id' not in columns:
        db.session.execute(text('ALTER TABLE users ADD COLUMN owner_id INTEGER'))
        db.session.commit()
    if 'campus' not in columns:
        # 旧版本没有校区字段，这里按旧版账号规则迁移一次，之后由用户手动维护。
        db.session.execute(text("ALTER TABLE users ADD COLUMN campus VARCHAR(20) NOT NULL DEFAULT 'baiyun'"))
        current_year = datetime.now(BJT).year
        legacy_users = db.session.execute(text('SELECT id, username FROM users')).fetchall()
        for user_id, username in legacy_users:
            campus = 'baiyun'
            try:
                campus = 'huizhou' if int(str(username)[:4]) >= current_year else 'baiyun'
            except (TypeError, ValueError):
                pass
            db.session.execute(text('UPDATE users SET campus = :campus WHERE id = :user_id'),
                                {'campus': campus, 'user_id': user_id})
        db.session.commit()


def _ensure_admin_account(app):
    """首次启动时创建管理员，并接管旧版本中没有 owner_id 的查寝账号。"""
    admin_email = (app.config.get('ADMIN_EMAIL') or '').strip().lower()
    if not admin_email:
        return

    admin = Account.query.filter_by(email=admin_email).first()
    if not admin:
        admin = Account(
            email=admin_email,
            password_hash=generate_password_hash(app.config['ADMIN_PASSWORD']),
            email_verified_at=datetime.now(BJT),
            role='admin',
            enabled=True,
        )
        db.session.add(admin)
        try:
            db.session.flush()
        except IntegrityError:
            # Gunicorn Worker 和 scheduler 进程可能同时初始化。
            # 查询完成后，其他进程可能已经创建了同一个管理员账号。
            db.session.rollback()
            admin = Account.query.filter_by(email=admin_email).first()
            if not admin:
                raise
    else:
        # 当前项目暂无独立的修改管理员密码页面，配置文件中的密码作为管理员密码来源。
        # 这样用户修改 .env 后重启服务即可生效，不会继续使用旧的密码哈希。
        if not admin.password_hash or not check_password_hash(admin.password_hash, app.config['ADMIN_PASSWORD']):
            admin.password_hash = generate_password_hash(app.config['ADMIN_PASSWORD'])

    # 迁移旧版创建的查寝账号，避免管理员升级后看不到原数据。
    User.query.filter(User.owner_id.is_(None)).update({'owner_id': admin.id}, synchronize_session=False)
    db.session.commit()
