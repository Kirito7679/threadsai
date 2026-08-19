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
from zoneinfo import ZoneInfo

os.environ.setdefault("SECRET_KEY", "smoke-test-secret-key-please-ignore")
os.environ.setdefault("DASHBOARD_PASSWORD", "test123")
os.environ.setdefault("ENABLE_SCHEDULER", "false")
# Нужен для проверки подписи signed_request в колбэках Meta
os.environ.setdefault("THREADS_APP_SECRET", "smoke-test-app-secret")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "smoke.db")

from fastapi.testclient import TestClient  # noqa: E402

import app.routes.ui as ui_module  # noqa: E402
from app import accounts, analytics, generator, publisher, research  # noqa: E402
from app.config import settings as app_settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    DeletionRequest,
    Draft,
    JobRun,
    Keyword,
    LlmUsage,
    Post,
    PostMetric,
    ResearchPost,
)
from app.security import encrypt  # noqa: E402
from app.threads_api import ThreadsAPIError  # noqa: E402

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

    print("\n7. Изоляция кабинетов")
    with session_scope() as db:
        stranger = Account(
            threads_user_id="99999999999",
            username="stranger",
            access_token_enc=encrypt("other-token"),
        )
        db.add(stranger)
        db.flush()
        foreign = Draft(
            account_id=stranger.id,
            status="pending",
            parts_json=json.dumps(["Черновик чужого аккаунта"], ensure_ascii=False),
        )
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id

    # Сессия принадлежит первому аккаунту — чужой черновик должен быть недоступен
    for path in (f"/queue/{foreign_id}/edit", f"/queue/{foreign_id}/delete"):
        response = client.post(
            path, data={"parts": "взлом"}, follow_redirects=False
        )
        check(
            f"чужой черновик защищён {path}",
            "error" in response.headers.get("location", ""),
            response.headers.get("location", ""),
        )

    with session_scope() as db:
        survivor = db.get(Draft, foreign_id)
        check("чужой черновик не изменён", survivor is not None and "чужого" in survivor.parts_json)

    print("\n8. Здоровье аккаунта")
    with session_scope() as db:
        broken = db.get(Account, account_id)
        check("аккаунт изначально здоров", broken.status == accounts.STATUS_OK)

        # Сетевые сбои копятся и не сразу выключают аккаунт
        for _ in range(accounts.MAX_CONSECUTIVE_ERRORS - 1):
            accounts.mark_api_error(broken, ThreadsAPIError(500, {"error": {"message": "5xx"}}))
        check(
            "сетевые сбои не выключают аккаунт сразу",
            broken.status == accounts.STATUS_OK and broken.consecutive_errors == 4,
            f"{broken.status}/{broken.consecutive_errors}",
        )
        accounts.mark_api_error(broken, ThreadsAPIError(500, {"error": {"message": "5xx"}}))
        check("череда сбоев уводит в needs_reauth", broken.status == accounts.NEEDS_REAUTH)

        accounts.mark_healthy(broken)
        check("успешный вызов чинит аккаунт", broken.status == accounts.STATUS_OK)

        # Ошибка авторизации выключает аккаунт с первого раза
        accounts.mark_api_error(broken, ThreadsAPIError(401, {"error": {"code": 190}}))
        check("ошибка авторизации выключает сразу", broken.status == accounts.NEEDS_REAUTH)

        db.flush()  # сессия с autoflush=False: без этого выборка не увидит статус
        check(
            "отвалившийся аккаунт не попадает в фоновые задачи",
            broken.id not in [a.id for a in accounts.get_all_accounts(db, only_healthy=True)],
        )
        check(
            "но виден в общем списке",
            broken.id in [a.id for a in accounts.get_all_accounts(db)],
        )
        accounts.mark_healthy(broken)

    print("\n9. Периодичность сбора метрик")
    now = datetime.now(timezone.utc)
    fresh = Post(published_at=now - timedelta(hours=2), metrics_updated_at=now - timedelta(minutes=20))
    check("свежий пост не опрашивается чаще часа", not analytics.needs_metrics(fresh, now))
    fresh.metrics_updated_at = now - timedelta(hours=2)
    check("свежий пост опрашивается через час", analytics.needs_metrics(fresh, now))

    week_old = Post(published_at=now - timedelta(days=3), metrics_updated_at=now - timedelta(hours=2))
    check("недельный пост ждёт 6 часов", not analytics.needs_metrics(week_old, now))
    week_old.metrics_updated_at = now - timedelta(hours=7)
    check("недельный пост опрашивается через 6 часов", analytics.needs_metrics(week_old, now))

    month_old = Post(published_at=now - timedelta(days=20), metrics_updated_at=now - timedelta(hours=10))
    check("месячный пост ждёт сутки", not analytics.needs_metrics(month_old, now))

    ancient = Post(published_at=now - timedelta(days=40), metrics_updated_at=None)
    check("старый пост не опрашивается вовсе", not analytics.needs_metrics(ancient, now))

    never = Post(published_at=now - timedelta(days=1), metrics_updated_at=None)
    check("ни разу не опрошенный пост берётся всегда", analytics.needs_metrics(never, now))

    print("\n10. Гонка синхронизации и публикации")
    with session_scope() as db:
        account = db.get(Account, account_id)
        racing = Draft(
            account_id=account.id,
            status="approved",
            topic="гонка",
            parts_json=json.dumps(["Пост, который синхронизация увидела первой"], ensure_ascii=False),
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        db.add(racing)
        db.flush()

        # Синхронизация успела завести строку раньше publisher'а
        db.add(
            Post(
                account_id=account.id,
                media_id="raced_media",
                text="Пост, который синхронизация увидела первой",
                published_at=datetime.now(timezone.utc),
            )
        )
        db.flush()

        post = publisher.record_post(db, account, "raced_media", racing, 0, "raced_media", racing_text := "Пост, который синхронизация увидела первой")
        db.flush()
        duplicates = db.query(Post).filter(Post.media_id == "raced_media").count()
        check("дубликат строки не создан", duplicates == 1, f"строк: {duplicates}")
        check("рубрика восстановлена", post.topic == "гонка", post.topic)
        check("черновик привязан", post.draft_id == racing.id)

    with session_scope() as db:
        account = db.get(Account, account_id)
        orphan = Draft(
            account_id=account.id,
            status="published",
            topic="усыновление",
            parts_json=json.dumps(["Мой старый пост про воронки продаж"], ensure_ascii=False),
        )
        db.add(orphan)
        db.flush()
        # own_1 приходит из заглушки get_own_posts и уже лежит без рубрики
        stray = db.query(Post).filter(Post.media_id == "own_1").first()
        stray.topic = ""
        stray.draft_id = None
        db.flush()

        analytics.sync_own_posts(db, account)
        db.flush()
        adopted = db.query(Post).filter(Post.media_id == "own_1").first()
        check("осиротевший пост привязан к черновику", adopted.draft_id == orphan.id, str(adopted.draft_id))
        check("рубрика подтянулась", adopted.topic == "усыновление", adopted.topic)

    print("\n11. Журнал задач и расход на модель")
    with session_scope() as db:
        mine = db.get(Account, account_id)
        other = db.query(Account).filter(Account.username == "stranger").first()
        db.add(JobRun(name="research", account_id=mine.id, status="ok", detail="мой запуск"))
        db.add(JobRun(name="research", account_id=other.id, status="error", detail="чужая ошибка"))
        db.add(LlmUsage(account_id=mine.id, job="generate", model="fake", prompt_tokens=1000, completion_tokens=500, cost_usd=0.00082))
        db.add(LlmUsage(account_id=other.id, job="generate", model="fake", prompt_tokens=9000, completion_tokens=9000, cost_usd=0.5))
        db.flush()

        jobs = ui_module.account_jobs(db, mine)
        check("журнал показывает только свои запуски", all(j.account_id == mine.id for j in jobs), str([j.account_id for j in jobs]))
        check("чужая ошибка не видна", all("чужая" not in (j.detail or "") for j in jobs))

        spend = analytics.llm_spend(db, mine.id)
        check("расход посчитан по своему аккаунту", abs(spend["cost_usd"] - 0.00082) < 1e-5, str(spend))
        check("чужой расход не приплюсован", spend["cost_usd"] < 0.5)
        check("прогноз на месяц считается", spend["monthly_usd"] >= 0)

    response = client.get("/settings")
    check("страница настроек с блоком расхода", response.status_code == 200 and "Расход на модель" in response.text)

    print("\n12. Ручной запуск задачи не трогает чужие аккаунты")
    from app.scheduler import run_job_for_account  # noqa: E402

    with session_scope() as db:
        mine_id = db.get(Account, account_id).id
        other_id = db.query(Account).filter(Account.username == "stranger").first().id
        before_mine = db.query(JobRun).filter(JobRun.account_id == mine_id).count()
        before_other = db.query(JobRun).filter(JobRun.account_id == other_id).count()

    run_job_for_account("research", mine_id)

    with session_scope() as db:
        after_mine = db.query(JobRun).filter(JobRun.account_id == mine_id).count()
        after_other = db.query(JobRun).filter(JobRun.account_id == other_id).count()
        check("свой аккаунт получил запуск", after_mine == before_mine + 1, f"{before_mine} -> {after_mine}")
        check("чужой аккаунт не тронут", after_other == before_other, f"{before_other} -> {after_other}")

        last = (
            db.query(JobRun)
            .filter(JobRun.account_id == mine_id)
            .order_by(JobRun.started_at.desc())
            .first()
        )
        check("запуск завершён, а не подвис", last.status in ("ok", "error"), last.status)
        check("запуск отмечен завершённым", last.finished_at is not None)

    check("неизвестная задача отклонена", "error" in run_job_for_account("нет-такой", mine_id))

    print("\n13. Изоляция сессий: чужой кабинет недоступен")
    from app.security import make_session_cookie  # noqa: E402

    with session_scope() as db:
        mine = db.get(Account, account_id)
        other = db.query(Account).filter(Account.username == "stranger").first()
        mine_uid, other_uid = mine.threads_user_id, other.threads_user_id
        mine_id, other_id = mine.id, other.id
        # У чужака должен быть валидный токен, иначе страница упадёт не на том
        other.access_token_enc = encrypt("stranger-token")
        db.flush()

    def session_client(payload: dict) -> TestClient:
        c = TestClient(app)
        c.cookies.set("session", make_session_cookie(payload))
        return c

    # Вход своим Threads-аккаунтом отдаёт свой кабинет
    c_mine = session_client({"auth": True, "uid": mine_uid, "owner": False})
    r = c_mine.get("/settings")
    check("свой кабинет открывается", r.status_code == 200 and "test_user" in r.text, str(r.status_code))
    check("чужой юзернейм не показан", "stranger" not in r.text)

    c_other = session_client({"auth": True, "uid": other_uid, "owner": False})
    r = c_other.get("/settings")
    check("чужак видит свой кабинет", r.status_code == 200 and "stranger" in r.text, str(r.status_code))
    check("а не первый аккаунт в базе", "test_user" not in r.text)

    # Сессия с неизвестным uid НЕ должна проваливаться в первый аккаунт
    c_ghost = session_client({"auth": True, "uid": "000000000000000", "owner": False})
    r = c_ghost.get("/", follow_redirects=False)
    check("неизвестный uid не пускает в чужой кабинет",
          r.status_code == 200 and "test_user" not in r.text, str(r.status_code))
    r = c_ghost.get("/queue", follow_redirects=False)
    check("и не даёт очередь", r.status_code == 303, str(r.status_code))

    # Чужой черновик недоступен даже по прямому номеру
    with session_scope() as db:
        victim = db.query(Draft).filter(Draft.account_id == mine_id).first()
        victim_id, victim_parts = victim.id, victim.parts_json
    r = c_other.post(f"/queue/{victim_id}/edit", data={"parts": "взлом"}, follow_redirects=False)
    check("чужак не редактирует мой черновик", "error" in r.headers.get("location", ""))
    with session_scope() as db:
        check("черновик не изменён", db.get(Draft, victim_id).parts_json == victim_parts)

    print("\n14. Вход по паролю")
    from app import security  # noqa: E402

    security.reset_failed_logins("testclient")
    bare = TestClient(app)
    r = bare.post("/auth/login", data={"password": "неверный"}, follow_redirects=False)
    check("неверный пароль отклонён", r.status_code == 401, str(r.status_code))

    for _ in range(4):
        bare.post("/auth/login", data={"password": "неверный"}, follow_redirects=False)
    r = bare.post("/auth/login", data={"password": "test123"}, follow_redirects=False)
    check("перебор блокируется даже с верным паролем", r.status_code == 429, str(r.status_code))

    security.reset_failed_logins("testclient")
    r = bare.post("/auth/login", data={"password": "test123"}, follow_redirects=False)
    check("после сброса верный пароль работает", r.status_code == 303, str(r.status_code))

    original_flag = security.settings.enable_password_login
    security.settings.enable_password_login = False
    security.reset_failed_logins("testclient")
    r = bare.post("/auth/login", data={"password": "test123"}, follow_redirects=False)
    check("выключенный вход по паролю не пускает", r.status_code == 403, str(r.status_code))
    r = bare.get("/auth/login")
    check("форма пароля скрыта", "Войти по паролю" not in r.text)
    security.settings.enable_password_login = original_flag

    original_pass = security.settings.dashboard_password
    security.settings.dashboard_password = "admin"
    check("пароль из примера не принимается", not security.password_login_available())
    security.settings.dashboard_password = original_pass
    security.reset_failed_logins("testclient")

    print("\n15. Колбэки Meta: подпись и настоящее удаление")
    import base64 as _b64  # noqa: E402
    import hashlib as _hash  # noqa: E402
    import hmac as _hmac  # noqa: E402

    def sign(payload: dict, secret: str) -> str:
        raw = _b64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        sig = _hmac.new(secret.encode(), raw.encode("ascii"), _hash.sha256).digest()
        return _b64.urlsafe_b64encode(sig).decode().rstrip("=") + "." + raw

    secret = security.settings.threads_app_secret
    good = {"algorithm": "HMAC-SHA256", "user_id": other_uid}

    r = bare.post("/auth/threads/delete", data={"signed_request": sign(good, "не-тот-секрет")})
    check("подделанная подпись отклонена", r.status_code == 400, str(r.status_code))
    with session_scope() as db:
        check("данные при этом целы", db.get(Account, other_id) is not None)

    r = bare.post("/auth/threads/delete", data={"signed_request": "мусор-без-точки"})
    check("мусор вместо signed_request отклонён", r.status_code == 400, str(r.status_code))

    with session_scope() as db:
        db.add(Draft(account_id=other_id, status="pending", parts_json=json.dumps(["чужой"])))
        db.add(Post(account_id=other_id, media_id="stranger_post", text="чужой пост"))
        db.flush()

    r = bare.post("/auth/threads/delete", data={"signed_request": sign(good, secret)})
    payload = r.json()
    check("валидная подпись принята", r.status_code == 200 and "confirmation_code" in payload, r.text[:120])
    code = payload.get("confirmation_code", "")
    check("вернулся адрес страницы статуса", code and code in payload.get("url", ""))

    with session_scope() as db:
        check("аккаунт удалён", db.get(Account, other_id) is None)
        check("черновики удалены", db.query(Draft).filter(Draft.account_id == other_id).count() == 0)
        check("посты удалены", db.query(Post).filter(Post.account_id == other_id).count() == 0)
        check("мой аккаунт не тронут", db.get(Account, mine_id) is not None)
        entry = db.query(DeletionRequest).filter(DeletionRequest.code == code).first()
        check("заявка сохранена со статусом done", entry is not None and entry.status == "done", entry.status if entry else "нет")

    r = bare.get(f"/data-deletion?code={code}")
    check("страница статуса показывает удаление", r.status_code == 200 and "Данные удалены" in r.text)

    # Отзыв доступа отключает аккаунт, но данные оставляет
    r = bare.post("/auth/threads/uninstall", data={"signed_request": sign({"algorithm": "HMAC-SHA256", "user_id": mine_uid}, secret)})
    check("отзыв доступа принят", r.status_code == 200, str(r.status_code))
    with session_scope() as db:
        acc = db.get(Account, mine_id)
        check("аккаунт отключён", acc is not None and not acc.is_active)
        check("но данные сохранены", db.query(Draft).filter(Draft.account_id == mine_id).count() > 0)
        acc.is_active = True
        acc.status = "ok"

    print("\n16. Часовой пояс у каждого аккаунта свой")
    from app.accounts import account_tz, set_setting  # noqa: E402
    from app.scheduler import work_generate_scheduled  # noqa: E402

    with session_scope() as db:
        account = db.get(Account, mine_id)
        set_setting(db, mine_id, "timezone", "Asia/Tokyo")
        set_setting(db, mine_id, "posting_hours", "9")
        set_setting(db, mine_id, "posts_per_day", "1")
        db.flush()
        check("пояс аккаунта читается", str(account_tz(db, mine_id)) == "Asia/Tokyo")

        slots = generator.propose_slots(db, account, 2)
        tokyo = ZoneInfo("Asia/Tokyo")
        hours_local = {s.astimezone(tokyo).hour for s in slots}
        check("слоты попадают в 9 утра по Токио", hours_local == {9}, str(hours_local))

        set_setting(db, mine_id, "timezone", "America/New_York")
        db.flush()
        slots_ny = generator.propose_slots(db, account, 2)
        ny = ZoneInfo("America/New_York")
        check("смена пояса сдвигает слоты",
              {s.astimezone(ny).hour for s in slots_ny} == {9}
              and slots_ny[0] != slots[0], str(slots_ny[0]))

        check("битый пояс откатывается к поясу сервиса",
              (set_setting(db, mine_id, "timezone", "Марс/Олимп"), db.flush(),
               str(account_tz(db, mine_id)))[2] == app_settings.timezone)
        set_setting(db, mine_id, "timezone", "Asia/Tokyo")
        db.flush()

    # Плановая генерация ждёт своего часа по местному времени
    with session_scope() as db:
        account = db.get(Account, mine_id)
        tokyo_hour = datetime.now(ZoneInfo("Asia/Tokyo")).hour
        set_setting(db, mine_id, "generation_hour", str((tokyo_hour + 3) % 24))
        db.flush()
        res = work_generate_scheduled(db, account)
        check("не его час — генерации нет", "skipped" in res, str(res))

        set_setting(db, mine_id, "generation_hour", str(tokyo_hour))
        db.flush()
        res = work_generate_scheduled(db, account)
        check("его час — генерация идёт", "created" in res, str(res))

    r = c_mine.post("/settings", data={"timezone": "Не/Существует"}, follow_redirects=False)
    check("битый пояс не сохраняется", "error" in r.headers.get("location", ""))
    r = c_mine.post("/settings", data={"generation_hour": "99"}, follow_redirects=False)
    check("час генерации вне диапазона отклонён", "error" in r.headers.get("location", ""))
    r = c_mine.post("/settings", data={"timezone": "Europe/Berlin", "generation_hour": "7"}, follow_redirects=False)
    check("корректные значения сохраняются", "ok=" in r.headers.get("location", ""))

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
