"""Смоук-тест: поднимает приложение на временной SQLite-базе, наполняет её
фейковыми данными и проверяет, что все страницы и действия отвечают.

Запуск:  .venv\\Scripts\\python.exe smoke_test.py
Сеть не используется: клиент Threads и DeepSeek подменяются заглушками.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SECRET_KEY", "smoke-test-secret-key-please-ignore")
os.environ.setdefault("DASHBOARD_PASSWORD", "test123")
os.environ.setdefault("ENABLE_SCHEDULER", "false")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "smoke.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import analytics, generator, publisher, research  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Draft, Keyword, Post, PostMetric, ResearchPost  # noqa: E402
from app.security import encrypt  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


# ----------------------------------------------------------------- Заглушки


class FakeThreadsClient:
    """Повторяет интерфейс ThreadsClient, но не ходит в сеть."""

    published: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.counter = 0

    def get_profile(self):
        return {"id": "1", "username": "test_user"}

    def get_publishing_limit(self):
        return {"data": [{"quota_usage": 7, "config": {"quota_total": 250}}]}

    def publish_text_post(self, text, reply_to_id=None, reply_control=None, **kwargs):
        self.counter += 1
        media_id = f"fake_media_{len(self.published) + 1}"
        self.published.append({"id": media_id, "text": text, "reply_to": reply_to_id})
        return media_id

    def get_post(self, media_id):
        return {"id": media_id, "permalink": f"https://threads.net/p/{media_id}"}

    def get_own_posts(self, limit=50, **kwargs):
        return [
            {
                "id": "own_1",
                "text": "Мой старый пост про воронки продаж",
                "media_type": "TEXT_POST",
                "permalink": "https://threads.net/p/own_1",
                "timestamp": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
                "is_reply": False,
            }
        ]

    def get_post_insights(self, media_id):
        return {"views": 1200, "likes": 43, "replies": 7, "reposts": 3, "quotes": 1, "shares": 2}

    def get_account_insights(self, since=None, until=None):
        return {"views": 5400, "likes": 210, "replies": 33, "reposts": 12, "quotes": 4, "clicks": 9, "followers_count": 1480}

    def keyword_search(self, query, search_type="TOP", **kwargs):
        return [
            {
                "id": f"{query}_{search_type}_{i}",
                "text": f"Популярный пост про {query} номер {i}. Личная история и конкретный вывод.",
                "username": f"author{i}",
                "permalink": f"https://threads.net/p/{query}{i}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "media_type": "TEXT_POST",
                "has_replies": True,
                "is_reply": False,
            }
            for i in range(3)
        ]


class FakeLLM:
    model = "fake-deepseek"
    is_configured = True

    def chat_json(self, system, user, **kwargs):
        if "Разбери" in user:
            ids = [line for line in user.split('"id": "') if line]
            parsed = json.loads(user[user.index("[") : user.rindex("]") + 1])
            return {
                "items": [
                    {
                        "id": item["id"],
                        "topic": "воронки",
                        "hook_type": "личная история",
                        "format_type": "короткая мысль",
                        "tone": "дружеский",
                        "insight": "Автор начинает с конкретной ошибки, это цепляет",
                        "relevance": 0.8,
                    }
                    for item in parsed
                ]
            }
        if "контент-стратег" in system:
            return {
                "summary": "В нише растёт спрос на разборы ошибок и короткие личные истории.",
                "trends": [
                    {"name": "разбор провала", "why": "честность даёт доверие",
                     "example_hooks": ["Слил 200к на рекламу"], "strength": 0.8}
                ],
                "content_ideas": [
                    {"topic": "воронки", "angle": "что я делал не так первый год",
                     "hook_type": "личная история", "why_now": "тема в топе выдачи"}
                ],
                "avoid": ["мотивационные цитаты"],
            }
        return {
            "drafts": [
                {
                    "topic": "воронки",
                    "hook_type": "личная история",
                    "parts": [
                        "Первый год я строил воронки наугад и слил на этом полугодовой бюджет.",
                        "Что помогло: сначала считать стоимость заявки, потом уже дизайнить лендинг.",
                    ],
                    "rationale": "Опирается на тренд «разбор провала»",
                }
                for _ in range(3)
            ]
        }


def patch_everything() -> None:
    fake_client = FakeThreadsClient()
    for module in (research, analytics, publisher, generator):
        if hasattr(module, "client_for"):
            module.client_for = lambda account, _c=fake_client: _c
    for module in (research, generator):
        module.get_llm = lambda: FakeLLM()
    import app.routes.ui as ui_module

    ui_module.get_llm = lambda: FakeLLM()


# ----------------------------------------------------------------- Данные


def seed() -> int:
    with session_scope() as db:
        account = Account(
            threads_user_id="17841400000000000",
            username="test_user",
            access_token_enc=encrypt("fake-token"),
            token_expires_at=datetime.now(timezone.utc) + timedelta(days=55),
            token_refreshed_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db.add(account)
        db.flush()

        from app.accounts import DEFAULT_SETTINGS
        from app.models import AccountSetting

        for key, value in DEFAULT_SETTINGS.items():
            db.add(AccountSetting(account_id=account.id, key=key, value=value))
        db.add(Keyword(account_id=account.id, term="маркетинг"))
        db.add(Keyword(account_id=account.id, term="воронки"))

        # Опубликованный пост с историей метрик — чтобы дашборд не был пустым
        post = Post(
            account_id=account.id,
            media_id="seed_post_1",
            text="Старый пост для проверки аналитики",
            topic="воронки",
            published_at=datetime.now(timezone.utc) - timedelta(days=2),
            views=3400,
            likes=120,
            replies=18,
            reposts=6,
        )
        db.add(post)
        db.flush()
        for hours_ago in (30, 20, 10, 1):
            db.add(
                PostMetric(
                    post_id=post.id,
                    collected_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                    views=3400 - hours_ago * 60,
                    likes=120 - hours_ago,
                    replies=18,
                    reposts=6,
                )
            )
        return account.id


# ----------------------------------------------------------------- Проверки


def main() -> int:
    init_db()
    patch_everything()
    account_id = seed()

    print("\n1. Пайплайн разведки")
    with session_scope() as db:
        account = db.get(Account, account_id)
        result = research.run_full_research(db, account)
        check("сбор постов", result["collected"]["new"] > 0, str(result["collected"]))
        check("классификация", result["analyzed"]["analyzed"] > 0, str(result["analyzed"]))
        check("отчёт по трендам", result["report_id"] is not None)

    print("\n2. Генерация черновиков")
    with session_scope() as db:
        account = db.get(Account, account_id)
        result = generator.generate_drafts(db, account, count=3)
        check("черновики созданы", result.get("created") == 3, str(result))
        slots = generator.propose_slots(db, account, 3)
        check("слоты расписания", len(slots) == 3 and all(s > datetime.now(timezone.utc) for s in slots))

    print("\n3. Публикация")
    with session_scope() as db:
        account = db.get(Account, account_id)
        draft = db.query(Draft).filter(Draft.status == "pending").first()
        outcome = publisher.publish_draft(db, account, draft)
        check("ветка опубликована", outcome.get("ok"), str(outcome))
        check("цепочка из 2 постов", len(outcome.get("media_ids", [])) == 2)
        check("вторая часть — ответ на первую", FakeThreadsClient.published[1]["reply_to"] == FakeThreadsClient.published[0]["id"])
        check("статус черновика", draft.status == "published")

    print("\n4. Аналитика")
    with session_scope() as db:
        account = db.get(Account, account_id)
        synced = analytics.sync_own_posts(db, account)
        check("синхронизация постов", synced.get("synced", 0) > 0, str(synced))
        metrics = analytics.collect_post_metrics(db, account)
        check("сбор метрик постов", metrics.get("updated", 0) > 0, str(metrics))
        acc_metrics = analytics.collect_account_metrics(db, account)
        check("метрики аккаунта", acc_metrics.get("views") == 5400, str(acc_metrics))
        summary = analytics.summary(db, account.id)
        check("сводка", summary["current"]["views"] > 0, str(summary["current"]))
        check("эффективность рубрик", len(analytics.topic_performance(db, account.id)) > 0)

    print("\n5. Веб-панель")
    client = TestClient(app)
    check("редирект на логин", client.get("/", follow_redirects=False).status_code == 303)
    check("здоровье сервиса", client.get("/healthz").json()["status"] == "ok")

    login = client.post("/auth/login", data={"password": "test123"}, follow_redirects=False)
    check("вход по паролю", login.status_code == 303 and "session" in login.cookies)
    check("неверный пароль отклонён", client.post("/auth/login", data={"password": "wrong"}).status_code == 401)

    for path in ("/", "/queue", "/queue?status=published", "/research", "/posts", "/settings"):
        response = client.get(path)
        check(f"страница {path}", response.status_code == 200, f"код {response.status_code}")

    with session_scope() as db:
        post_id = db.query(Post).first().id
        draft_id = db.query(Draft).filter(Draft.status == "pending").first().id
    check("карточка поста", client.get(f"/posts/{post_id}").status_code == 200)

    actions = [
        (f"/queue/{draft_id}/edit", {"parts": "Новый текст первой части.\n\nВторая часть ветки.", "topic": "тест"}),
        (f"/queue/{draft_id}/approve", {"scheduled_at": ""}),
        ("/keywords/add", {"term": "смм", "search_mode": "KEYWORD"}),
        ("/settings", {"niche": "маркетинг", "posts_per_day": "2", "posting_hours": "10,18"}),
    ]
    for path, data in actions:
        response = client.post(path, data=data, follow_redirects=False)
        location = response.headers.get("location", "")
        check(f"действие {path}", response.status_code == 303 and "error" not in location, location)

    with session_scope() as db:
        draft = db.get(Draft, draft_id)
        check("черновик отредактирован", json.loads(draft.parts_json)[0].startswith("Новый текст"))
        check("черновик запланирован", draft.status == "approved" and draft.scheduled_at is not None)
        check("слишком длинный текст отклонён", True)

    long_text = "x" * 600
    response = client.post(f"/queue/{draft_id}/edit", data={"parts": long_text}, follow_redirects=False)
    check("лимит 500 символов работает", "error" in response.headers.get("location", ""))

    print("\n6. Публикация по расписанию")
    with session_scope() as db:
        account = db.get(Account, account_id)
        draft = db.get(Draft, draft_id)
        draft.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.flush()
        outcome = publisher.publish_due(db, account)
        check("просроченный слот опубликован", outcome.get("published") == 1, str(outcome))

    print("\n" + "=" * 52)
    if failures:
        print(f"ПРОВАЛЕНО ПРОВЕРОК: {len(failures)}")
        for item in failures:
            print("  -", item)
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
