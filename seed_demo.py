"""Наполняет ЛОКАЛЬНУЮ базу демо-данными, чтобы посмотреть панель до подключения Threads.

Запуск:  .venv\\Scripts\\python.exe seed_demo.py

Создаёт демо-аккаунт, историю постов с метриками, отчёт по трендам и черновики.
Черновики пишутся настоящим DeepSeek, если задан DEEPSEEK_API_KEY.

⚠️  ПЕРЕД ПОДКЛЮЧЕНИЕМ НАСТОЯЩЕГО АККАУНТА удалите файл data/app.db —
    иначе демо-аккаунт останется в базе и панель будет показывать его.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.accounts import DEFAULT_SETTINGS
from app.db import init_db, session_scope
from app.models import (
    Account,
    AccountMetric,
    AccountSetting,
    Draft,
    Keyword,
    Post,
    PostMetric,
    ResearchPost,
    TrendReport,
)
from app.security import encrypt

DEMO_USERNAME = "demo_account"

DEMO_POSTS = [
    ("Первый год я строил воронки наугад и слил на этом полугодовой бюджет.", "личный опыт"),
    ("Ошибка, которая стоила мне 100 тысяч: вложился в рекламу, не проверив спрос.", "разбор ошибки"),
    ("Три вопроса клиенту, после которых становится ясно, купит он или нет.", "полезный список"),
    ("Заметил закономерность: чем длиннее лендинг, тем хуже конверсия в заявку.", "наблюдение"),
    ("Что тебе сейчас сложнее всего в продвижении? Разберу частые боли в постах.", "вопрос аудитории"),
    ("Перестал делать скидки и продажи выросли. Объясняю, почему так вышло.", "разбор ошибки"),
    ("Клиент ушёл к конкуренту с ценой втрое выше. Пошёл выяснять причину.", "личный опыт"),
    ("Пять признаков, что ваш маркетолог не понимает продукт.", "полезный список"),
    ("Год вёл блог без единой продажи. Что поменял на тринадцатый месяц.", "личный опыт"),
    ("Самый недооценённый канал в 2026 — это ваша база старых клиентов.", "наблюдение"),
    ("Считаю стоимость заявки каждую неделю. Вот таблица, которой пользуюсь.", "полезный список"),
    ("Почему «сарафан» перестаёт работать ровно тогда, когда на него понадеялся.", "наблюдение"),
]

DEMO_RESEARCH = [
    ("marketolog_pro", "Слил 200к на таргет и понял главное: сначала оффер, потом трафик.", "провал", "личная история"),
    ("biz_notes", "Разобрал 40 лендингов конкурентов. Показываю, что общего у трёх лучших.", "разбор", "цифра"),
    ("smm_daily", "Никто не покупает с первого касания. Норма — семь. Считайте иначе.", "воронка", "инсайт"),
    ("startup_kitchen", "Уволил подрядчика по рекламе и сделал сам. Заявки подешевели вдвое.", "провал", "провокация"),
    ("content_lab", "Формат, который стабильно собирает охваты: короткая история с цифрой в первой строке.", "формат", "список"),
    ("prodazhi_channel", "Спросил у 50 клиентов, почему выбрали нас. Ответ удивил.", "исследование", "вопрос"),
]

TREND_PAYLOAD = {
    "summary": (
        "В нише растёт спрос на честные разборы провалов с конкретными суммами. "
        "Абстрактные советы почти не собирают охват, личный опыт с цифрами — собирает. "
        "Заметен сдвиг в сторону коротких постов на 2-3 предложения вместо длинных тредов."
    ),
    "trends": [
        {"name": "разбор собственного провала", "why": "честность с цифрами вызывает доверие быстрее любой экспертности",
         "example_hooks": ["Слил 200к на рекламу", "Ошибка, которая стоила мне года"], "strength": 0.9},
        {"name": "цифра в первой строке", "why": "конкретное число останавливает скролл лучше вопроса",
         "example_hooks": ["Разобрал 40 лендингов", "Спросил у 50 клиентов"], "strength": 0.75},
        {"name": "контринтуитивный вывод", "why": "ломает ожидание и заставляет дочитать",
         "example_hooks": ["Перестал делать скидки и продажи выросли"], "strength": 0.6},
    ],
    "content_ideas": [
        {"topic": "воронки", "angle": "что я делал не так в первый год", "hook_type": "личная история",
         "why_now": "тема стабильно в топе выдачи"},
        {"topic": "цены", "angle": "почему клиент выбрал вариант втрое дороже", "hook_type": "провокация",
         "why_now": "у конкурентов заходит тема ценообразования"},
        {"topic": "аналитика", "angle": "минимальный набор метрик для микробизнеса", "hook_type": "полезный список",
         "why_now": "запрос на простые инструменты без сложных систем"},
    ],
    "avoid": ["мотивационные цитаты", "«5 секретов успеха»", "прогнозы на год без конкретики"],
    "stats": {
        "posts_in_window": 6,
        "popular_posts": 6,
        "top_topics": [["провал", 2], ["разбор", 1], ["воронка", 1], ["формат", 1], ["исследование", 1]],
        "top_hooks": [["личная история", 2], ["цифра", 1], ["инсайт", 1], ["провокация", 1], ["вопрос", 1]],
        "top_formats": [["короткая мысль", 4], ["разбор", 2]],
    },
    "generated_by": "демо-данные",
}


def main() -> int:
    init_db()
    random.seed(42)
    now = datetime.now(timezone.utc)

    with session_scope() as db:
        if db.scalars(select(Account).where(Account.username == DEMO_USERNAME)).first():
            print("Демо-аккаунт уже есть. Чтобы пересоздать, удалите data/app.db и запустите снова.")
            return 0

        account = Account(
            threads_user_id="demo-0000000000",
            username=DEMO_USERNAME,
            access_token_enc=encrypt("demo-token-not-valid"),
            token_expires_at=now + timedelta(days=58),
            token_refreshed_at=now - timedelta(days=2),
        )
        db.add(account)
        db.flush()

        values = dict(DEFAULT_SETTINGS)
        values["niche"] = "запуск и продвижение малого бизнеса"
        values["audience"] = "предприниматели 25-40, делают первые продажи, бюджет на маркетинг небольшой"
        for key, value in values.items():
            db.add(AccountSetting(account_id=account.id, key=key, value=value))

        for term in ("маркетинг", "воронки продаж", "малый бизнес", "таргет", "конверсия"):
            db.add(Keyword(account_id=account.id, term=term))

        # Посты за 30 дней с историей метрик
        for index, (text, topic) in enumerate(DEMO_POSTS):
            published = now - timedelta(days=29 - index * 2, hours=random.randint(0, 8))
            views = random.randint(800, 9000)
            likes = int(views * random.uniform(0.02, 0.06))
            post = Post(
                account_id=account.id,
                media_id=f"demo_post_{index}",
                root_media_id=f"demo_post_{index}",
                text=text,
                topic=topic,
                permalink=f"https://www.threads.net/@{DEMO_USERNAME}/post/demo{index}",
                published_at=published,
                views=views,
                likes=likes,
                replies=int(likes * random.uniform(0.1, 0.4)),
                reposts=int(likes * random.uniform(0.05, 0.2)),
                quotes=random.randint(0, 4),
                shares=random.randint(0, 8),
                metrics_updated_at=now,
            )
            db.add(post)
            db.flush()

            # Кривая набора просмотров: быстро в первые часы, потом плато
            for step, share in enumerate((0.35, 0.62, 0.81, 0.93, 1.0)):
                db.add(
                    PostMetric(
                        post_id=post.id,
                        collected_at=published + timedelta(hours=step * 6),
                        views=int(views * share),
                        likes=int(likes * share),
                        replies=int(post.replies * share),
                        reposts=int(post.reposts * share),
                    )
                )

        # Дневные метрики аккаунта
        followers = 820
        for day_offset in range(29, -1, -1):
            day = now - timedelta(days=day_offset)
            followers += random.randint(2, 28)
            db.add(
                AccountMetric(
                    account_id=account.id,
                    day=day.strftime("%Y-%m-%d"),
                    views=random.randint(1500, 12000),
                    likes=random.randint(60, 400),
                    replies=random.randint(5, 60),
                    reposts=random.randint(2, 30),
                    quotes=random.randint(0, 6),
                    clicks=random.randint(10, 90),
                    followers_count=followers,
                    collected_at=day,
                )
            )

        # Разведка и отчёт по трендам
        for index, (username, text, topic, hook) in enumerate(DEMO_RESEARCH):
            db.add(
                ResearchPost(
                    account_id=account.id,
                    threads_post_id=f"demo_research_{index}",
                    keyword=random.choice(("маркетинг", "воронки продаж", "малый бизнес")),
                    search_type="TOP",
                    username=username,
                    text=text,
                    permalink=f"https://www.threads.net/@{username}",
                    posted_at=now - timedelta(hours=random.randint(2, 60)),
                    collected_at=now - timedelta(hours=1),
                    analyzed=True,
                    topic=topic,
                    hook_type=hook,
                    format_type="короткая мысль",
                    tone="дружеский",
                    insight="Начинает с конкретной цифры или потери — это останавливает скролл.",
                    relevance=round(random.uniform(0.6, 0.95), 2),
                )
            )

        report = TrendReport(
            account_id=account.id,
            window_hours=72,
            posts_analyzed=len(DEMO_RESEARCH),
            payload_json=json.dumps(TREND_PAYLOAD, ensure_ascii=False),
            created_at=now - timedelta(hours=1),
        )
        db.add(report)
        db.flush()
        account_id = account.id

    # Черновики: настоящая генерация, если ключ DeepSeek на месте
    from app.generator import generate_drafts
    from app.llm import get_llm

    if get_llm().is_configured:
        print("Генерирую черновики через DeepSeek...")
        with session_scope() as db:
            account = db.get(Account, account_id)
            result = generate_drafts(db, account, count=4)
            print("  результат:", result)
    else:
        print("DEEPSEEK_API_KEY не задан — добавляю запасные черновики без ИИ")
        with session_scope() as db:
            for topic, parts in (
                ("личный опыт", ["Год назад я считал, что дело в продукте. Оказалось — в том, что о нём никто не знал."]),
                ("разбор ошибки", ["Запустил рекламу без проверки спроса. 40 тысяч в никуда.", "Теперь сначала смотрю поисковые запросы, потом трачу бюджет."]),
            ):
                db.add(
                    Draft(
                        account_id=account_id,
                        status="pending",
                        parts_json=json.dumps(parts, ensure_ascii=False),
                        topic=topic,
                        hook_type="личная история",
                        rationale="Запасной черновик из демо-набора",
                        model="демо",
                    )
                )

    print("\nГотово. Открывайте http://localhost:8000")
    print("⚠️  Перед подключением настоящего аккаунта Threads удалите data/app.db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
