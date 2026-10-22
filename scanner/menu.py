"""Bot screens: menu, status, top, cards, settings. Plain functions returning (HTML, keyboard).

Inline screens are edited in place (callback "m:<screen>[:args]"), actions are "a:<action>[:args]"
and re-render a screen. The reply keyboard at the bottom (MAIN_KEYBOARD) opens the same screens.
Listing buttons under alerts stay "c:/h:/w:<source>:<id>", price picks "p:<index>:<price>".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

from aiogram.types import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
)

from scanner import config_edit, learn, overrides
from scanner.config import PROJECT_ROOT, AppConfig, Settings, load_config
from scanner.db import Database, Status
from scanner.gemini import PRAGUE, GeminiReviewer, Review, Verdict
from scanner.logs import tail
from scanner.notify import NO_AI_EMOJI, VERDICT_EMOJI, age_words, format_alert, format_pc_alert, kc
from scanner.pc import Components, PcSpecs
from scanner.pipeline import cached_pc_results, value_pc_listing
from scanner.pricing import Evaluation, PriceList, evaluate, evaluate_many, max_price_rule_lines, rank

log = logging.getLogger(__name__)

Screen = tuple[str, InlineKeyboardMarkup | None]

# --- reply keyboard and command list -----------------------------------------------------------

BTN_STATUS, BTN_TOP, BTN_SKIPPED = "📊 Статус", "🏆 Топ", "🚫 Отбраковано"
BTN_SETTINGS, BTN_PAUSE, BTN_LOGS = "⚙️ Настройки", "⏯ Пауза / старт", "📜 Логи"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_TOP)],
        [KeyboardButton(text=BTN_SKIPPED), KeyboardButton(text=BTN_SETTINGS)],
        [KeyboardButton(text=BTN_PAUSE), KeyboardButton(text=BTN_LOGS)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Меню снизу или /menu",
)

BOT_COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="status", description="Статус: прогоны, ошибки, Gemini"),
    BotCommand(command="top", description="Лучшие кандидаты за 24 ч"),
    BotCommand(command="skipped", description="Что отбраковал Gemini"),
    BotCommand(command="settings", description="Потолок покупки, радиус, поиски"),
    BotCommand(command="prices", description="Отчёт по ценам: что поправить в прайсе"),
    BotCommand(command="logs", description="Последние ошибки"),
    BotCommand(command="pause", description="Остановить сканирование"),
    BotCommand(command="resume", description="Продолжить сканирование"),
    BotCommand(command="price", description="Поправить цену: /price iphone 15 128 9500"),
    BotCommand(command="search", description="Поиски: list / add / remove"),
    BotCommand(command="help", description="Справка"),
]

HELP = """\
<b>🤖 Сканер Bazoš</b>

Кнопки внизу открывают разделы, кнопки под сообщениями переключают их на месте.

<b>Команды</b>
/menu · главное меню
/status · прогоны, ошибки, лимиты Gemini
/top · лучшие кандидаты за 24 ч и «залежавшиеся»
/skipped · что Gemini отбраковал и почему
/settings · потолок покупки, радиус, поиски, отчёт по ценам
/prices · отчёт по ценам сейчас (сам приходит по воскресеньям)
/logs · последние ошибки; /logs fb · что делал Facebook
/pause, /resume · остановить и продолжить
/price iphone 15 128 9500 · поправить market_price
/search add mobil galaxy s24 3000 20000 · добавить поиск
/pct 2 80 · ступень 2 потолка → 80 %
/radius 60 · радиус, км

<b>Кнопки под объявлением</b>
✍️ Написал · сообщу, если объявление пропадёт
🙈 Скрыть · больше не присылать
🔁 Снизится · сообщу при падении цены от 5 %"""


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


HOME = _btn("🏠 Меню", "m:home")


def alert_keyboard(source: str, listing_id: str, pressed: str | None = None) -> InlineKeyboardMarkup:
    labels = {"c": "✍️ Написал", "h": "🙈 Скрыть", "w": "🔁 Снизится, сообщи"}
    row = [_btn(("✅ " if key == pressed else "") + text, f"{key}:{source}:{listing_id}") for key, text in labels.items()]
    return InlineKeyboardMarkup(inline_keyboard=[row[:2], row[2:]])


# --- helpers ------------------------------------------------------------------------------------


def _fmt_ts(value: str | None) -> str:
    """DB timestamps are naive local server time (UTC on the host): show them in Prague time."""
    if not value:
        return "—"
    return datetime.fromisoformat(value).astimezone(PRAGUE).strftime("%d.%m. %H:%M")


def code_version() -> str:
    """Commit of the running code: env from the host (GIT_COMMIT, RENDER_GIT_COMMIT, ...) or .git, else '?'."""
    for name in ("GIT_COMMIT", "SOURCE_COMMIT", "RENDER_GIT_COMMIT", "RAILWAY_GIT_COMMIT_SHA", "COMMIT_SHA"):
        if value := os.environ.get(name):
            return value[:7]
    head = PROJECT_ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            ref_file = PROJECT_ROOT / ".git" / ref[5:]
            if ref_file.exists():
                return ref_file.read_text().strip()[:7]
            packed = (PROJECT_ROOT / ".git" / "packed-refs").read_text()
            return next(l.split()[0][:7] for l in packed.splitlines() if l.endswith(ref[5:]))
        return ref[:7]
    except (OSError, StopIteration):
        return "?"


def _paused(db: Database) -> bool:
    return db.get_state("paused") == "1"


def _pause_button(db: Database, back: str) -> InlineKeyboardButton:
    return _btn("▶️ Продолжить", f"a:resume:{back}") if _paused(db) else _btn("⏸ Пауза", f"a:pause:{back}")


def _duration(run) -> str:
    dur = int((datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds())
    return f"{dur // 60} мин {dur % 60} с" if dur >= 60 else f"{dur} с"


# --- screens ------------------------------------------------------------------------------------


def screen_home(cfg: AppConfig, db: Database, next_run: datetime | None = None) -> Screen:
    state = "⏸ на паузе" if _paused(db) else "▶️ работает"
    runs = db.last_runs(1)
    last = (f"последний прогон {_fmt_ts(runs[0]['started_at'])}, кандидатов {runs[0]['candidates']}"
            if runs and runs[0]["finished_at"] else "прогонов ещё не было")
    nxt = f"\n⏭ следующий в {next_run.astimezone(PRAGUE):%H:%M}" if next_run and not _paused(db) else ""
    text = f"<b>🏠 Меню</b>\n{state} · {last}{nxt}"
    return text, _kb(
        [_btn("📊 Статус", "m:status"), _btn("🏆 Топ", "m:top")],
        [_btn("🚫 Отбраковано", "m:skipped"), _btn("📜 Логи", "m:logs")],
        [_btn("⚙️ Настройки", "m:settings"), _pause_button(db, "home")],
    )


def cmd_status(settings: Settings, cfg: AppConfig, db: Database, next_run: datetime | None = None) -> str:
    prices = PriceList.load(cfg.pricing.prices_path)
    state = "⏸ на паузе" if _paused(db) else "▶️ работает"
    lines = ["<b>📊 Статус</b>", f"{state} · версия {code_version()} · прайс {len(prices.items)} позиций", ""]
    runs = db.last_runs(1)
    if runs:
        r = runs[0]
        if r["finished_at"]:
            lines.append(f"🕐 Последний прогон: {_fmt_ts(r['started_at'])} ({_duration(r)})")
        else:
            lines.append(f"🔄 Прогон идёт с {_fmt_ts(r['started_at'])}")
        lines.append(f"📦 Объявлений {r['listings']} · новых {r['new']} · цена изменилась {r['price_changed']}")
        lines.append(f"🎯 Кандидатов {r['candidates']} · уведомлений {r['alerts']}")
        if r["errors"]:
            lines.append("❗ Ошибки: " + escape(r["errors"][:800]))
    else:
        lines.append("Прогонов ещё не было")
    if next_run and not _paused(db):
        lines.append(f"⏭ Следующий: {next_run.astimezone(PRAGUE):%H:%M}")
    c = db.counts()
    lines += [
        "",
        f"🗂 В базе {c['total']} · уведомлено {c.get('alerted', 0)} · написал {c.get('contacted', 0)} · "
        f"скрыто {c.get('hidden', 0)} · слежу за ценой {c['watched']}",
        f"📍 Bazoš: {cfg.bazos.radius_km} км · {len(cfg.bazos.searches)} поисков · каждые {cfg.notify.interval_min} мин"
        f" · тихие часы {cfg.notify.quiet_start}-{cfg.notify.quiet_end}",
    ]
    lines.append(fb_status_line(settings, cfg, db))
    reviewer = GeminiReviewer(settings, cfg.gemini, cfg.pricing, PriceList([]), db)
    if reviewer.disabled_reason:
        lines.append(f"🤖 Gemini: {reviewer.disabled_reason}")
    for model, u in reviewer.usage().items():
        lines.append(f"🤖 {model}: {u['requests']} запросов, осталось {u['left']} из {settings.gemini_rpd}"
                     + (f", ошибок {u['errors']}" if u["errors"] else ""))
    return "\n".join(lines)


def fb_status_line(settings: Settings, cfg: AppConfig, db: Database) -> str:
    if not settings.fb_enabled:
        return "📘 Facebook: выключен (FB_ENABLED)"
    if db.get_state("fb_blocked") == "1":
        return "📘 Facebook: ⚠️ ждёт входа: fb-login на ПК и пришли боту fb_state.json"
    if error := db.get_state("fb_setup_error"):
        return f"📘 Facebook: ⚠️ {escape(error[:200])}"
    runs = db.last_runs(1, source="fb")
    last = (f"последний прогон {_fmt_ts(runs[0]['started_at'])}, объявлений {runs[0]['listings']}"
            if runs and runs[0]["finished_at"] else "прогонов ещё не было")
    return f"📘 Facebook: каждые ~{cfg.facebook.interval_min} мин, {last}"


def screen_status(settings: Settings, cfg: AppConfig, db: Database, next_run: datetime | None = None) -> Screen:
    fb_row = [_btn("🔁 Проверить FB", "a:fbretry")] if settings.fb_enabled and db.get_state("fb_blocked") == "1" else []
    return cmd_status(settings, cfg, db, next_run), _kb(
        [_btn("🔄 Обновить", "m:status"), _pause_button(db, "status")], fb_row, [HOME])


@dataclass
class TopItems:
    active: list[Evaluation]
    stale: list[Evaluation]

    @property
    def all(self) -> list[Evaluation]:
        return self.active + self.stale


def top_items(cfg: AppConfig, db: Database, now: datetime | None = None, limit: int = 10, stale_limit: int = 5) -> TopItems:
    now = now or datetime.now()
    prices = PriceList.load(cfg.pricing.prices_path)
    listings = db.listings(seen_since=now - timedelta(hours=24))
    hidden = {(l.source, l.id) for l in listings if (row := db.get(l.source, l.id)) and row["status"] == Status.HIDDEN}
    today = now.date()
    evaluations = evaluate_many(listings, prices, cfg.pricing, today)
    pcs = [r.evaluation for r in cached_pc_results(evaluations, db, prices, cfg, today) if not r.skipped]
    ranked = [e for e in sorted(rank(evaluations) + pcs, key=lambda e: e.score, reverse=True)
              if (e.listing.source, e.listing.id) not in hidden]
    return TopItems([e for e in ranked if "stale" not in e.flags][:limit],
                    [e for e in ranked if "stale" in e.flags][:stale_limit])


def _review_emoji(db: Database, source: str, listing_id: str, price: int | None) -> str:
    row = db.get_review(source, listing_id, price) if price is not None else None
    return VERDICT_EMOJI[row["verdict"]] if row else NO_AI_EMOJI


def cmd_top(cfg: AppConfig, db: Database, now: datetime | None = None, items: TopItems | None = None) -> str:
    now = now or datetime.now()
    items = items or top_items(cfg, db, now)
    today = now.date()

    def line(i: int, e: Evaluation) -> str:
        l = e.listing
        icon = "🖥" if e.match.item.category == "pc" else _review_emoji(db, l.source, l.id, l.price)
        return (f"<b>{i}.</b> {icon} <a href=\"{escape(l.url)}\">{escape(e.match.item.label)}</a> · <b>{kc(l.price)} Kč</b>\n"
                f"      рынок {kc(e.market_price)} · макс {kc(e.max_price)} · предложить {kc(e.offer_price)}\n"
                f"      {escape(l.location or '?')}, {age_words(l.posted_date, today)} · score {e.score}")

    out = [f"<b>🏆 Топ за 24 ч: {len(items.active)}</b>"]
    out += [line(i, e) for i, e in enumerate(items.active, 1)] or ["кандидатов нет"]
    if items.stale:
        out.append(f"\n<b>🕸 Залежавшиеся (&gt; {cfg.pricing.stale_days} дн.)</b>")
        out += [line(i, e) for i, e in enumerate(items.stale, len(items.active) + 1)]
    if items.all:
        out.append("\n🟢 брать · 🟡 торговаться · 🔴 Gemini против · ⚪ без проверки · 🖥 ПК\n"
                   "Номер ниже откроет карточку с кнопками.")
    text = "\n".join(out)
    return text if len(text) <= 4000 else text[:3990].rsplit("\n<b>", 1)[0] + "\n…"


def screen_top(cfg: AppConfig, db: Database, now: datetime | None = None) -> Screen:
    items = top_items(cfg, db, now)
    numbers = [_btn(str(i), f"m:card:{e.listing.source}:{e.listing.id}") for i, e in enumerate(items.all, 1)]
    rows = [numbers[i:i + 5] for i in range(0, len(numbers), 5)]
    return cmd_top(cfg, db, now, items), _kb(*rows, [_btn("🔄 Обновить", "m:top"), HOME])


def card_text(cfg: AppConfig, db: Database, source: str, listing_id: str, now: datetime | None = None) -> str | None:
    """Full alert-style card: the last alert text, else built from the rules (and a cached Gemini answer)."""
    row = db.conn.execute(
        "SELECT text FROM alerts WHERE source = ? AND listing_id = ? AND kind != 'gone' ORDER BY id DESC LIMIT 1",
        (source, listing_id),
    ).fetchone()
    if row:
        return row["text"]
    listing = next((l for l in db.listings() if l.source == source and l.id == listing_id), None)
    if listing is None:
        return None
    today = (now or datetime.now()).date()
    prices = PriceList.load(cfg.pricing.prices_path)
    ev = evaluate(listing, prices, cfg.pricing, today)
    if ev.match is None and (spec_row := db.get_pc_specs(source, listing_id)):
        specs = PcSpecs.model_validate_json(spec_row["response"])
        result = value_pc_listing(ev, specs, Components.load(cfg.pricing.components_path), prices, cfg, today)
        if result.valuation and result.valuation.market is not None:
            return format_pc_alert("deal", result.evaluation, specs, result.valuation, today)[0]
    if ev.match is None or listing.price is None:
        return None
    review = db.get_review(source, listing_id, listing.price)
    rv = (Review(ev, Verdict.model_validate_json(review["response"]), review["model"], cached=True) if review
          else Review(ev, no_ai_reason="Gemini это объявление ещё не проверял"))
    return format_alert("deal", rv, cfg.pricing, today)[0]


def screen_card(cfg: AppConfig, db: Database, source: str, listing_id: str) -> Screen:
    text = card_text(cfg, db, source, listing_id) or "Объявления нет в базе"
    buttons = alert_keyboard(source, listing_id).inline_keyboard
    return text, _kb(*buttons, [_btn("⬅️ К топу", "m:top"), HOME])


def cmd_skipped(db: Database, now: datetime | None = None, limit: int = 15) -> str:
    rows = db.reviews((now or datetime.now()) - timedelta(hours=24), only_hidden=True)[:limit]
    if not rows:
        return "<b>🚫 Отбраковано</b>\nGemini за 24 ч ничего не отбраковал"
    out = [f"<b>🚫 Отбраковано Gemini за 24 ч: {len(rows)}</b>"]
    for r in rows:
        v = Verdict.model_validate_json(r["response"])
        risk = f" · риск {v.scam_risk}" if v.scam_risk != "low" else ""
        out.append(f"\n{VERDICT_EMOJI[v.verdict]} <a href=\"{escape(r['url'])}\">{escape(r['title'])}</a> · "
                   f"<b>{kc(r['price'])} Kč</b>{risk}\n      {escape(v.reason_ru)}")
    return "\n".join(out)


def screen_skipped(db: Database) -> Screen:
    return cmd_skipped(db), _kb([_btn("🔄 Обновить", "m:skipped"), HOME])


def cmd_logs(settings: Settings, lines: int = 30) -> str:
    rows = tail(settings.log_dir / "errors.log", lines)
    if not rows:
        return "<b>📜 Логи</b>\nВ логе ошибок пусто 👍"
    text = "\n".join(rows)[-3500:]  # Telegram: 4 096 characters per message
    return f"<b>📜 Последние ошибки и предупреждения</b>\n<pre>{escape(text)}</pre>"


def cmd_fb_logs(settings: Settings, lines: int = 40) -> str:
    """What the last FB runs did (INFO lines of scanner.log): pages opened, listings found and dropped."""
    rows = [r for r in tail(settings.log_dir / "scanner.log", 3000)
            if " scanner.sources.facebook:" in r or " scanner.browser_setup:" in r
            or ("scanner.bot:" in r and " fb" in r)][-lines:]
    if not rows:
        return "<b>📘 Лог Facebook</b>\nFB-прогонов в логе ещё нет"
    text = "\n".join(r.split(" ", 2)[-1] if r[:4].isdigit() else r for r in rows)[-3500:]
    return f"<b>📘 Лог Facebook</b>\n<pre>{escape(text)}</pre>"


def screen_logs(settings: Settings) -> Screen:
    return cmd_logs(settings), _kb([_btn("🔄 Обновить", "m:logs"), _btn("📘 Лог FB", "m:fblogs")], [HOME])


def screen_fb_logs(settings: Settings) -> Screen:
    return cmd_fb_logs(settings), _kb([_btn("🔄 Обновить", "m:fblogs"), _btn("📜 Ошибки", "m:logs")], [HOME])


# --- settings -----------------------------------------------------------------------------------

RADIUS_OPTIONS = (30, 45, 60, 80, 100)


def screen_settings(cfg: AppConfig) -> Screen:
    n = cfg.notify
    rule = "\n".join(f"• {escape(l)}" for l in max_price_rule_lines(cfg.pricing)[:len(cfg.pricing.max_price_tiers)])
    text = (
        "<b>⚙️ Настройки</b>\n\n"
        f"<b>📐 Потолок покупки</b>\n{rule}\n\n"
        f"<b>📍 Радиус:</b> {cfg.bazos.radius_km} км от {cfg.bazos.location}\n"
        f"<b>🔎 Поиски:</b> {len(cfg.bazos.searches)}\n"
        f"<b>🔔 Уведомления:</b> объявления не старше {n.max_age_days} дн. или со снижением цены, "
        f"залежавшиеся (&gt; {cfg.pricing.stale_days} дн.) только в топе; до {n.max_alerts_per_run} за прогон\n"
        f"<b>🌙 Тихие часы:</b> {n.quiet_start}-{n.quiet_end}, утром сводка\n\n"
        "<i>Изменения действуют со следующего прогона и сохраняются в базе: после передеплоя бот их восстановит "
        "(если в репозитории это значение не поменяли).</i>"
    )
    return text, _kb(
        [_btn("📐 Потолок покупки", "m:tiers"), _btn("📍 Радиус", "m:radius")],
        [_btn("🔎 Поиски", "m:searches"), _btn("📈 Отчёт по ценам", "m:prices")],
        [HOME],
    )


def screen_tiers(cfg: AppConfig, note: str = "") -> Screen:
    lines = "\n".join(f"• {escape(l)}" for l in max_price_rule_lines(cfg.pricing))
    text = f"<b>📐 Потолок покупки</b>\n{lines}" + (f"\n\n{escape(note)}" if note else "") + \
        "\n\n<i>➖ / ➕ меняют процент ступени на 1. KNOWLEDGE.md при этом не меняется.</i>"
    rows = [[_btn("➖", f"a:tier:{i}:-1"), _btn(f"Ступень {i}: {t.pct} %", "a:noop"), _btn("➕", f"a:tier:{i}:1")]
            for i, t in enumerate(cfg.pricing.max_price_tiers, 1)]
    return text, _kb(*rows, [_btn("⬅️ Настройки", "m:settings"), HOME])


def screen_radius(cfg: AppConfig) -> Screen:
    cur = cfg.bazos.radius_km
    text = f"<b>📍 Радиус поиска</b>\nСейчас {cur} км от {cfg.bazos.location}. Другое значение: /radius 70"
    options = sorted(set(RADIUS_OPTIONS) | {cur})
    return text, _kb([_btn(("✅ " if km == cur else "") + f"{km}", f"a:radius:{km}") for km in options],
                     [_btn("⬅️ Настройки", "m:settings"), HOME])


def screen_searches(cfg: AppConfig) -> Screen:
    lines = [f"{i}. {escape(s.name)} · {s.section} · {kc(s.price_min)}-{kc(s.price_max)} Kč"
             for i, s in enumerate(cfg.bazos.searches, 1)]
    text = ("<b>🔎 Поиски Bazoš</b>\n" + "\n".join(lines) +
            "\n\nДобавить: <code>/search add mobil galaxy s24 3000 20000</code>\nУдалить: кнопка ❌ ниже")
    buttons = [_btn(f"❌ {i}. {s.name}"[:30], f"m:rmsearch:{i}") for i, s in enumerate(cfg.bazos.searches, 1)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return text, _kb(*rows, [_btn("⬅️ Настройки", "m:settings"), HOME])


def screen_remove_search(cfg: AppConfig, index: int) -> Screen:
    if not 1 <= index <= len(cfg.bazos.searches):
        return screen_searches(cfg)
    s = cfg.bazos.searches[index - 1]
    return (f"Удалить поиск «{escape(s.name)}»?",
            _kb([_btn("✅ Удалить", f"a:rmsearch:{index}"), _btn("Отмена", "m:searches")]))


# --- price learning report ---------------------------------------------------------------------


def report_keyboard(report_id: int, has_open: bool) -> InlineKeyboardMarkup:
    rows = [[_btn("✅ Применить всё", f"a:papply:{report_id}:all"), _btn("☑️ Выборочно", f"m:pselect:{report_id}")]] \
        if has_open else []
    return _kb(*rows, [_btn("🔄 Пересчитать", "m:prices"), _btn("⬅️ Настройки", "m:settings")], [HOME])


def screen_prices(settings: Settings, db: Database) -> Screen:
    """A fresh report from the observations (also what the weekly job sends)."""
    report_id, report, cfg = learn.new_report(settings, db)
    return learn.format_report(report, cfg.learn), report_keyboard(report_id, bool(report.proposals))


def screen_report(db: Database, report_id: int, note: str = "") -> Screen:
    rows = learn.proposals(db, report_id)
    lines = [f"<b>📈 Отчёт №{report_id}</b>"]
    for p in rows:
        state = "✅ применено" if p["applied_at"] else ("☑️" if p["selected"] else "⬜")
        lines.append(f"{p['n']}. {escape(p['item'])}: {kc(p['old_price'])} → <b>{kc(p['new_price'])}</b> · {state}")
    if note:
        lines.append(f"\n{escape(note)}")
    return "\n".join(lines), report_keyboard(report_id, any(not p["applied_at"] for p in rows))


def screen_pselect(db: Database, report_id: int) -> Screen:
    rows = [p for p in learn.proposals(db, report_id) if not p["applied_at"]]
    text = f"<b>☑️ Выбери, что применить (отчёт №{report_id})</b>\nНажатие переключает позицию."
    buttons = [[_btn(f"{'✅' if p['selected'] else '⬜'} {p['n']}. {p['item']} → {kc(p['new_price'])}"[:60],
                     f"a:ptoggle:{report_id}:{p['n']}")] for p in rows[:20]]
    return text, _kb(*buttons, [_btn("✅ Применить выбранные", f"a:papply:{report_id}:sel")],
                     [_btn("⬅️ Отчёт", f"m:preport:{report_id}"), HOME])


# --- text commands (also used from the menu) ----------------------------------------------------


def cmd_pct(settings: Settings, db: Database, args: str | None) -> str:
    if args:
        parts = args.split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return "Формат: <code>/pct 2 80</code> (ступень, процент)"
        try:
            overrides.set_tier_pct(settings, db, int(parts[0]), int(parts[1]))
            cfg = load_config(settings.config_path)
        except config_edit.ConfigEditError as exc:
            return escape(str(exc))
        head = f"Ступень {parts[0]} → {parts[1]} %. Новые цифры со следующего прогона."
    else:
        cfg, head = load_config(settings.config_path), "Потолок покупки (config.yaml):"
    rule = "\n".join(f"{i}. {escape(l)}" for i, l in enumerate(max_price_rule_lines(cfg.pricing), 1))
    return f"{head}\n{rule}\n\nKNOWLEDGE.md обнови вручную, если поменял ступени."


def cmd_radius(settings: Settings, db: Database, args: str | None) -> str:
    if not args or not args.strip().isdigit():
        return f"Радиус сейчас {load_config(settings.config_path).bazos.radius_km} км. Формат: <code>/radius 60</code>"
    try:
        overrides.set_radius(settings, db, int(args))
    except config_edit.ConfigEditError as exc:
        return escape(str(exc))
    return f"Радиус Bazoš: {int(args)} км (со следующего прогона)"


def cmd_search(settings: Settings, db: Database, args: str | None) -> str:
    parts = (args or "list").split()
    action, rest = parts[0], parts[1:]
    try:
        if action == "list":
            return screen_searches(load_config(settings.config_path))[0]
        if action == "add":
            if len(rest) < 2 or rest[0] not in ("mobil", "pc"):
                return "Формат: <code>/search add mobil galaxy s24 3000 20000</code>"
            section, words = rest[0], rest[1:]
            nums = []
            while words and words[-1].isdigit() and len(nums) < 2:
                nums.insert(0, int(words.pop()))
            if not words:
                return "Нужен текст запроса"
            lo, hi = (nums + [None, None])[:2]
            overrides.add_search(settings, db, section, " ".join(words), lo, hi)
            return f"Добавлен поиск «{escape(' '.join(words))}» [{section}]"
        if action == "remove" and rest:
            name = overrides.remove_search(settings, db, " ".join(rest))
            return f"Удалён поиск «{escape(name)}»"
    except config_edit.ConfigEditError as exc:
        return escape(str(exc))
    return "Формат: /search list | add &lt;mobil|pc&gt; &lt;запрос&gt; [от] [до] | remove &lt;имя|номер&gt;"


def apply_price(settings: Settings, db: Database, index: int, price: int) -> str:
    try:
        label, old = overrides.set_market_price(settings, db, index, price)
    except config_edit.ConfigEditError as exc:
        return escape(str(exc))
    log.warning("market_price changed via Telegram: %s %d -> %d", label, old, price)
    return f"💰 {escape(label)}: {kc(old)} → {kc(price)} Kč (prices.json)"


def cmd_price(settings: Settings, db: Database, args: str | None) -> Screen:
    parts = (args or "").split()
    if len(parts) < 2 or not parts[-1].isdigit():
        return "Формат: <code>/price iphone 15 pro 128 15500</code>", None
    query, price = " ".join(parts[:-1]), int(parts[-1])
    cfg = load_config(settings.config_path)
    hits = config_edit.find_price_items(cfg.pricing.prices_path, query)
    if not hits:
        return f"В прайсе нет «{escape(query)}»", None
    if len(hits) == 1:
        return apply_price(settings, db, hits[0].index, price), None
    if len(hits) > 12:
        return f"Нашлось {len(hits)} позиций, уточни запрос", None
    rows = [[_btn(f"{h.label} ({kc(h.market_price)})", f"p:{h.index}:{price}")] for h in hits]
    return f"Какую позицию поставить на {kc(price)} Kč?", InlineKeyboardMarkup(inline_keyboard=rows)


# --- dispatch -----------------------------------------------------------------------------------


def render(name: str, args: list[str], settings: Settings, db: Database, next_run: datetime | None = None) -> Screen:
    """Screen by its callback name ("m:<name>:<args>")."""
    cfg = load_config(settings.config_path)
    if name == "status":
        return screen_status(settings, cfg, db, next_run)
    if name == "top":
        return screen_top(cfg, db)
    if name == "card" and len(args) == 2:
        return screen_card(cfg, db, args[0], args[1])
    if name == "skipped":
        return screen_skipped(db)
    if name == "logs":
        return screen_logs(settings)
    if name == "fblogs":
        return screen_fb_logs(settings)
    if name == "settings":
        return screen_settings(cfg)
    if name == "tiers":
        return screen_tiers(cfg)
    if name == "radius":
        return screen_radius(cfg)
    if name == "searches":
        return screen_searches(cfg)
    if name == "prices":
        return screen_prices(settings, db)
    if name == "preport" and args and args[0].isdigit():
        return screen_report(db, int(args[0]))
    if name == "pselect" and args and args[0].isdigit():
        return screen_pselect(db, int(args[0]))
    if name == "rmsearch" and args and args[0].isdigit():
        return screen_remove_search(cfg, int(args[0]))
    return screen_home(cfg, db, next_run)


def act(action: str, args: list[str], settings: Settings, db: Database) -> tuple[str, str, list[str]]:
    """Run a menu action; returns (toast text, screen to show, its args)."""
    if action in ("pause", "resume"):
        db.set_state("paused", "1" if action == "pause" else "0")
        return ("⏸ Пауза" if action == "pause" else "▶️ Сканирование продолжается"), (args[0] if args else "home"), []
    try:
        if action == "fbretry":
            db.set_state("fb_blocked", "0")
            return "Проверяю FB в ближайшие секунды", "status", []
        if action == "ptoggle" and len(args) == 2:
            learn.toggle(db, int(args[0]), int(args[1]))
            return "", "pselect", [args[0]]
        if action == "papply" and len(args) == 2:
            notes = learn.apply(settings, db, int(args[0]), only_selected=args[1] == "sel")
            return (f"Применено: {sum('→' in n for n in notes)}" if notes else "Нечего применять"), "preport", [args[0]]
        if action == "tier" and len(args) == 2:
            cfg = load_config(settings.config_path)
            i = int(args[0])
            pct = cfg.pricing.max_price_tiers[i - 1].pct + int(args[1])
            overrides.set_tier_pct(settings, db, i, pct)
            return f"Ступень {i}: {pct} %", "tiers", []
        if action == "radius" and args:
            overrides.set_radius(settings, db, int(args[0]))
            return f"Радиус {args[0]} км", "radius", []
        if action == "rmsearch" and args:
            cfg = load_config(settings.config_path)
            name = cfg.bazos.searches[int(args[0]) - 1].name
            overrides.remove_search(settings, db, name)
            return f"Удалён поиск «{name}»", "searches", []
    except (config_edit.ConfigEditError, IndexError, ValueError) as exc:
        return str(exc)[:190], "settings", []
    return "", "home", []
