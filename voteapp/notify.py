"""模拟通知适配器：不发送真实短信/邮件，统一写入 outbox 表，
在“通知记录”页可查看，便于演示与测试断言。"""
from .services import iso, utcnow

EVENT_LABEL = {
    "topic_announced": "议题公示通知",
    "proxy_registered": "代理授权登记通知",
    "transfer_registered": "过户登记通知",
    "result_published": "表决结果公布通知",
}


class MockNotifier:
    def __init__(self, db):
        self.db = db

    def send(self, user_id, event, payload, now=None):
        now = now or utcnow()
        import json
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO outbox(at,user_id,event,payload) VALUES(?,?,?,?)",
                (iso(now), user_id, event, json.dumps(payload, ensure_ascii=False)))

    def broadcast(self, user_ids, event, payload, now=None):
        now = now or utcnow()
        for uid in user_ids:
            self.send(uid, event, payload, now)
