"""模拟通知适配器：不接入真实短信/邮件，只落库并打印，便于演示与测试。"""
import json

from .db import connect


class MockNotifier:
    def __init__(self, db_path, echo=True):
        self.db_path = db_path
        self.echo = echo

    def send(self, user_id, template, payload):
        conn = connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO notifications (user_id, template, payload, "
                    "created_at) VALUES (?,?,?, datetime('now','localtime'))",
                    (user_id, template, json.dumps(payload, ensure_ascii=False)))
        finally:
            conn.close()
        if self.echo:
            print(f"[notify] -> 用户#{user_id} [{template}] "
                  f"{json.dumps(payload, ensure_ascii=False)}", flush=True)

    def sent_to(self, user_id):
        conn = connect(self.db_path)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM notifications WHERE user_id=? ORDER BY id",
                (user_id,)).fetchall()]
        finally:
            conn.close()
