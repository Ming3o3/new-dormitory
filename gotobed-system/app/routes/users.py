from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_required, current_user
import re

from ..models import db, User, Log
from .auth import EMAIL_RE, _issue_code, _verify_code
from ..crypto import encrypt_password
from ..scheduler import add_user_job, remove_user_job, update_user_job


users_bp = Blueprint('users', __name__)

CRON_PRESETS = {
    '10 21 * * *': '每天 21:10（北京时间）',
    '30 21 * * *': '每天 21:30（北京时间）',
    '10 22 * * *': '每天 22:10（北京时间）',
    '30 22 * * *': '每天 22:30（北京时间）',
}
CAMPUSES = {'baiyun': '白云校区', 'huizhou': '惠州校区'}
MAX_MANAGED_USERS_PER_ACCOUNT = 1


def _form_email(form):
    """规范化通知邮箱，便于比较新旧地址。"""
    return (form.get('email') or '').strip().lower()


def _ajax_response(message, ok):
    """验证码接口统一返回 JSON，避免表单页面刷新。"""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes.best == 'application/json':
        return jsonify({'ok': ok, 'message': message})
    flash(message, 'success' if ok else 'danger')
    return redirect(url_for('users.user_new'))


def _validate_cron_time(expr: str) -> bool:
    """验证 cron 表达式格式：分 时 * * *（0-59 0-23）"""
    m = re.match(r'^(\d{1,2}) (\d{1,2}) \* \* \*$', expr.strip())
    if not m:
        return False
    minute, hour = int(m.group(1)), int(m.group(2))
    return 0 <= minute <= 59 and 0 <= hour <= 23


def _cron_time_sort_key(expr: str):
    """按小时和分钟排序，保证同一组时间生成稳定的任务顺序。"""
    fields = expr.split()
    try:
        return int(fields[1]), int(fields[0]), expr
    except (IndexError, ValueError):
        return 99, 99, expr


def _parse_cron_times(form) -> list:
    """从表单解析所有打卡时间（预设 + 自定义）"""
    times = form.getlist('cron_times')
    custom = form.get('custom_time', '').strip()
    if custom:
        # 自定义时间格式 HH:MM -> cron 表达式
        match = re.match(r'^(\d{1,2}):(\d{2})$', custom)
        if match:
            h, m = match.group(1), match.group(2)
            times.append(f'{m} {h} * * *')
    # 去重 + 验证
    valid = []
    seen = set()
    for t in times:
        t = t.strip()
        if t not in seen and _validate_cron_time(t):
            valid.append(t)
            seen.add(t)
    return sorted(valid, key=_cron_time_sort_key)


def _managed_user_count():
    """返回当前登录账号已创建的查寝账号数量。"""
    return User.query.filter_by(owner_id=current_user.id).count()


def _user_limit_reached():
    # 管理员用于集中管理多个查寝账号，不受单账号数量限制。
    return not current_user.is_admin and _managed_user_count() >= MAX_MANAGED_USERS_PER_ACCOUNT


@users_bp.route('/')
@login_required
def user_list():
    query = User.query if current_user.is_admin else User.query.filter_by(owner_id=current_user.id)
    users = query.order_by(User.created_at.desc()).all()
    can_add_user = not _user_limit_reached()
    enabled_count = sum(1 for user in users if user.enabled)
    log_query = Log.query.join(User)
    if not current_user.is_admin:
        log_query = log_query.filter(User.owner_id == current_user.id)
    # 概览只统计最近 20 次执行，避免很早的历史结果影响当前状态判断。
    recent_logs = log_query.order_by(Log.executed_at.desc()).limit(20).all()
    success_logs = sum(1 for log in recent_logs if log.status == 'success')
    stats = {
        'total_users': len(users),
        'enabled_users': enabled_count,
        'disabled_users': len(users) - enabled_count,
        'success_rate': round(success_logs / len(recent_logs) * 100) if recent_logs else 0,
    }
    return render_template('users/list.html', users=users, stats=stats, can_add_user=can_add_user)


@users_bp.route('/users/send-notification-code', methods=['POST'])
@login_required
def send_notification_code():
    """向通知邮箱发送验证码；只有验证通过后才会保存该邮箱。"""
    email = _form_email(request.form)
    if not EMAIL_RE.match(email):
        return _ajax_response('请输入有效的通知邮箱', False)
    error = _issue_code(email, 'notify_email')
    return _ajax_response(error or '验证码已发送，请查收邮件', not error)


@users_bp.route('/users/new', methods=['GET', 'POST'])
@login_required
def user_new():
    # 普通登录账号最多绑定一个查寝账号；管理员可集中添加多个账号。
    if _user_limit_reached():
        flash('每个登录账号只能添加一个查寝账号', 'danger')
        return redirect(url_for('users.user_list'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        email = _form_email(request.form)
        campus = request.form.get('campus', '').strip()
        if not username or not password:
            flash('账号和密码为必填项', 'danger')
            return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=None, form_email=email)

        if campus not in CAMPUSES:
            flash('请选择查寝校区', 'danger')
            return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=None, form_email=email)

        if email:
            ok, error = _verify_code(email, 'notify_email', request.form.get('notification_code'))
            if not ok:
                flash(f'通知邮箱验证失败：{error}', 'danger')
                return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=None, form_email=email)

        user = User(
            owner_id=current_user.id if not current_user.is_admin else current_user.id,
            username=username,
            password_encrypted=encrypt_password(password),
            principal=request.form.get('principal', '').strip() or None,
            credential=request.form.get('credential', '').strip() or None,
            email=email or None,
            campus=campus,
            enabled='enabled' in request.form,
        )
        cron_times = _parse_cron_times(request.form)
        if not cron_times:
            flash('请至少选择一个打卡时间', 'danger')
            return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=None, form_email=email)

        # 再次检查，覆盖用户打开表单后其他请求先完成创建的情况。
        if _user_limit_reached():
            flash('每个登录账号只能添加一个查寝账号', 'danger')
            return redirect(url_for('users.user_list'))

        user.set_cron_times(cron_times)
        db.session.add(user)
        db.session.commit()

        if user.enabled:
            add_user_job(user)

        flash(f'用户 {username} 添加成功', 'success')
        return redirect(url_for('users.user_list'))

    return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=None)


@users_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def user_edit(user_id):
    user = User.query.get_or_404(user_id)
    if not current_user.is_admin and user.owner_id != current_user.id:
        from flask import abort
        abort(403)

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        email = _form_email(request.form)
        campus = request.form.get('campus', '').strip()
        if not username:
            flash('账号为必填项', 'danger')
            return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=user, form_email=email)

        if campus not in CAMPUSES:
            flash('请选择查寝校区', 'danger')
            return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=user, form_email=email)

        user.username = username
        if password:
            user.password_encrypted = encrypt_password(password)
        user.principal = request.form.get('principal', '').strip() or None
        user.credential = request.form.get('credential', '').strip() or None
        old_email = (user.email or '').strip().lower()
        if email and email != old_email:
            ok, error = _verify_code(email, 'notify_email', request.form.get('notification_code'))
            if not ok:
                flash(f'通知邮箱验证失败：{error}', 'danger')
                return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=user, form_email=email)
        user.email = email or None
        user.campus = campus
        cron_times = _parse_cron_times(request.form)
        if not cron_times:
            flash('请至少选择一个打卡时间', 'danger')
            return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=user, form_email=email)
        user.set_cron_times(cron_times)
        user.enabled = 'enabled' in request.form
        db.session.commit()

        update_user_job(user)

        flash(f'用户 {username} 更新成功', 'success')
        return redirect(url_for('users.user_list'))

    return render_template('users/form.html', presets=CRON_PRESETS, campuses=CAMPUSES, user=user)


@users_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
def user_delete(user_id):
    user = User.query.get_or_404(user_id)
    if not current_user.is_admin and user.owner_id != current_user.id:
        from flask import abort
        abort(403)
    username = user.username
    remove_user_job(user_id)
    db.session.delete(user)
    db.session.commit()
    flash(f'用户 {username} 已删除', 'success')
    return redirect(url_for('users.user_list'))


@users_bp.route('/users/<int:user_id>/toggle', methods=['POST'])
@login_required
def user_toggle(user_id):
    user = User.query.get_or_404(user_id)
    if not current_user.is_admin and user.owner_id != current_user.id:
        from flask import abort
        abort(403)
    user.enabled = not user.enabled
    db.session.commit()

    update_user_job(user)

    status = '启用' if user.enabled else '禁用'
    flash(f'用户 {user.username} 已{status}', 'success')
    return redirect(url_for('users.user_list'))


@users_bp.route('/users/<int:user_id>/test', methods=['POST'])
@login_required
def user_test(user_id):
    """立即执行查寝测试，返回 JSON 结果"""
    from flask import jsonify
    user = User.query.get_or_404(user_id)
    if not current_user.is_admin and user.owner_id != current_user.id:
        from flask import abort
        abort(403)
    from ..crypto import decrypt_password
    from ..tasks.gotobed import run_gotobed

    password = decrypt_password(user.password_encrypted)
    result = run_gotobed(
        username=user.username,
        password=password,
        principal=user.principal,
        credential=user.credential,
        email=user.email,
        campus=user.campus,
    )

    # 记录日志
    from ..models import Log
    log = Log(user_id=user.id, status=result['status'], message=result['message'])
    db.session.add(log)
    db.session.commit()

    return jsonify(result)
