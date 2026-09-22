FROM python:3.11-slim

# 安装 Node.js（PyExecJS 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖文件并安装
COPY gotobed-system/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制管理系统代码
COPY gotobed-system/ .

# 复制原脚本的 JS 文件（密码加密依赖）— 使用英文路径避免编码问题
COPY "gzlg助手/g5116.js" ./g5116.js

# 创建数据目录
RUN mkdir -p /app/data

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "2", "--worker-class", "gthread", "--timeout", "30", "--graceful-timeout", "30", "--keep-alive", "2", "--max-requests", "1000", "--max-requests-jitter", "100", "--access-logfile", "-", "--error-logfile", "-", "run:app"]
