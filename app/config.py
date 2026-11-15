import os
from pathlib import Path

class Config:
    SECRET = "cc-flow-lab-dev"
    LAB_NAME = "编排流抽取台"
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    # 后台入库/回滚工作线程的模拟耗时（秒），默认即时；验收争用可经环境变量调大
    LANDER_DELAY = float(os.environ.get("LANDER_DELAY", "0") or 0)
    ROLLBACK_DELAY = float(os.environ.get("ROLLBACK_DELAY", "0") or 0)
    @staticmethod
    def database() -> str:
        p = os.environ.get("DATABASE_PATH") or str(Path(__file__).resolve().parent.parent / "instance" / "flow_lab.db")
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        return p
