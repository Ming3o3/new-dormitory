"""Docker 调度器服务使用的 APScheduler 单实例入口。"""

import signal
import time

from run import app


_running = True


def _stop(_signum, _frame):
    global _running
    _running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


if __name__ == '__main__':
    app.logger.info('独立调度器已启动')
    while _running:
        time.sleep(1)
