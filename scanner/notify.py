"""Telegram message texts (HTML parse mode). Pure functions, no network."""

from __future__ import annotations

from datetime import date
from html import escape

from scanner.config import PricingConfig
from scanner.gemini import Review
from scanner.models import Listing
from scanner.pc import PcSpecs, PcValuation, breakdown_lines
from scanner.pricing import Evaluation

SOURCE_NAMES = {"bazos": "Bazoš", "fb": "FB"}
VERDICT_EMOJI = {"buy": "🟢", "negotiate": "🟡", "skip": "🔴"}
NO_AI_EMOJI = "⚪"
TELEGRAM_LIMIT = 4096

_FLAG_NAMES = {
    "shop": "магазин/сервис",
    "возможен обмен": "возможен обмен",
}


def kc(value: int | None) -> str:
    return "—" if value is None else f"{value:,}".replace(",", " ")


def age_words(posted: date | None, today: date) -> str:
    if posted is None:
        return "дата ?"
    days = (today - posted).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} дн. назад"


def liquidity_dots(liquidity: int) -> str:
    n = max(1, min(3, liquidity))
    return "●" * n + "○" * (3 - n)


def _model(review: Review) -> str:
    v, ev = review.verdict, review.evaluation
    if v and v.model:
        return f"{v.model} {v.variant}".strip()
    return ev.match.item.label if ev.match else ev.listing.title


def _emoji(review: Review) -> str:
    return VERDICT_EMOJI[review.verdict.verdict] if review.verdict else NO_AI_EMOJI


def offer_and_max(review: Review) -> tuple[int | None, int | None]:
    v, ev = review.verdict, review.evaluation
    return (v.offer_price, v.max_price) if v else (ev.offer_price, ev.max_price)


def _flags(review: Review, today: date) -> list[str]:
    ev, v = review.evaluation, review.verdict
    out = []
    for f in ev.flags:
        if f == "stale" and ev.listing.posted_date:
            out.append(f"висит {(today - ev.listing.posted_date).days} дн.")
        elif f in _FLAG_NAMES:
            out.append(_FLAG_NAMES[f])
    if ev.match and ev.match.confidence == "low":
        out.append("вариант не указан: взят самый дешёвый")
    if ev.match and ev.match.item.confidence == "estimate":
        out.append("цена прайса — оценка")
    if v:
        out += v.red_flags
        if v.scam_risk != "low":
            out.append(f"риск мошенничества: {v.scam_risk}")
    if review.rematched:
        out.append(f"позиция прайса исправлена: {review.rematched}")
    unique: dict[str, str] = {}
    for f in out:
        unique.setdefault(f.casefold(), f)  # "возможен обмен" from the rules and from Gemini once
    return list(unique.values())


def fallback_message(listing: Listing, offer: int | None) -> str:
    """Seller message without Gemini, by the KNOWLEDGE.md template."""
    price = f" Šlo by to za {offer} Kč při osobním předání?" if offer else ""
    return f"Dobrý den, je inzerát ještě aktuální?{price} Děkuji."


def format_alert(
    kind: str, review: Review, pricing: PricingConfig, today: date, old_price: int | None = None
) -> tuple[str, str]:
    """(full message, one-line digest summary) for a candidate. kind: deal / drop / watch."""
    ev, v = review.evaluation, review.verdict
    listing, item = ev.listing, ev.match.item
    price = listing.price
    market = review.adjusted_market
    offer, max_price = offer_and_max(review)
    emoji = _emoji(review)
    drop = f"📉 {kc(old_price)} → " if kind in {"drop", "watch"} and old_price else ""
    where = ", ".join(x for x in (SOURCE_NAMES.get(listing.source, listing.source), listing.location,
                                  age_words(listing.posted_date, today)) if x)

    lines = [f"{emoji} {drop}<b>{escape(_model(review))} — {kc(price)} Kč</b>  ({escape(where)})"]
    if kind == "watch":
        lines.insert(0, "🔁 Цена снизилась (ты просил сообщить)")
    elif kind == "drop":
        lines.insert(0, "📉 Снижение цены")
    lines.append(f"Рынок ~{kc(market)} · предложить {kc(offer)} · максимум {kc(max_price)}")
    if market is not None and price is not None and offer is not None and max_price is not None:
        worst, best = market - min(price, max_price), market - offer
        profit = kc(best) if worst >= best else f"{kc(worst)}–{kc(best)}"
        lines.append(f"Прибыль ~{profit} · ликвидность {liquidity_dots(item.liquidity)}")
    if v and v.condition_notes:
        lines.append("📋 " + escape(" · ".join(v.condition_notes)))
    flags = _flags(review, today)
    lines.append("⚠️ флаги: " + (escape("; ".join(flags)) if flags else "—"))
    if v:
        lines.append(f"🤖 {escape(v.reason_ru)}")
    else:
        lines.append(f"🤖 без ИИ: {escape(review.no_ai_reason or 'нет ответа Gemini')}")
    message = v.message_cs if v and v.message_cs else fallback_message(listing, offer)
    lines.append(f"💬 <code>{escape(message)}</code>")
    lines.append(f"🔗 {escape(listing.url)}")

    summary = (
        f"{emoji} {drop}<a href=\"{escape(listing.url)}\">{escape(_model(review))}</a> — {kc(price)} Kč · "
        f"предложить {kc(offer)} · макс {kc(max_price)} · {escape(listing.location or '?')}"
    )
    return "\n".join(lines), summary


def pc_message(specs: PcSpecs, offer: int | None) -> str:
    """Seller message for a PC (KNOWLEDGE.md template): ask for what the listing does not say."""
    unknown = [name for name, known in (("základní desky", specs.motherboard), ("zdroje", specs.psu)) if not known]
    ask = f" Mohl byste napsat přesný model {' a '.join(unknown)}?" if unknown else ""
    price = f" Šlo by to za {offer} Kč při osobním předání?" if offer else ""
    return f"Dobrý den, je PC ještě k dispozici?{price}{ask} Byla grafická karta použitá na těžbu? Děkuji."


def format_pc_alert(
    kind: str, ev: Evaluation, specs: PcSpecs, valuation: PcValuation, today: date, old_price: int | None = None,
) -> tuple[str, str]:
    """(full message, digest line) for a ready-built PC valued from its components."""
    listing, item = ev.listing, ev.match.item
    price, market, offer, max_price = listing.price, ev.market_price, ev.offer_price, ev.max_price
    emoji = "🟢" if price is not None and max_price is not None and price <= max_price else "🟡"
    drop = f"📉 {kc(old_price)} → " if kind == "drop" and old_price else ""
    where = ", ".join(x for x in (SOURCE_NAMES.get(listing.source, listing.source), listing.location,
                                  age_words(listing.posted_date, today)) if x)
    name = f"ПК {item.variant}" if item.variant else "ПК"
    lines = [f"🖥 {emoji} {drop}<b>{escape(name)} — {kc(price)} Kč</b>  ({escape(where)})"]
    if kind == "drop":
        lines.insert(0, "📉 Снижение цены")
    lines += [escape(line) for line in breakdown_lines(valuation)]
    lines.append(f"Рынок ~{kc(market)} · предложить {kc(offer)} · максимум {kc(max_price)}")
    if None not in (market, price, offer, max_price):
        worst, best = market - min(price, max_price), market - offer
        profit = kc(best) if worst >= best else f"{kc(worst)}–{kc(best)}"
        lines.append(f"Прибыль ~{profit} · ликвидность {liquidity_dots(item.liquidity)}")
    if specs.condition_notes:
        lines.append("📋 " + escape(" · ".join(specs.condition_notes)))
    flags = [f"висит {(today - listing.posted_date).days} дн." for f in ev.flags if f == "stale" and listing.posted_date]
    flags += [_FLAG_NAMES[f] for f in ev.flags if f in _FLAG_NAMES]
    flags += valuation.flags + specs.red_flags
    if specs.is_oem:
        flags.append(f"OEM-сборка (−{valuation.oem_discount_pct} %)")
    if specs.scam_risk != "low":
        flags.append(f"риск мошенничества: {specs.scam_risk}")
    unique: dict[str, str] = {}
    for f in flags:
        unique.setdefault(f.casefold(), f)
    lines.append("⚠️ флаги: " + (escape("; ".join(unique.values())) if unique else "—"))
    lines.append("🤖 железо прочитал Gemini, цена по таблице компонентов (knowledge/components.json)")
    lines.append(f"💬 <code>{escape(pc_message(specs, offer))}</code>")
    lines.append(f"🔗 {escape(listing.url)}")
    summary = (f"🖥 {emoji} {drop}<a href=\"{escape(listing.url)}\">{escape(name)}</a> — {kc(price)} Kč · "
               f"предложить {kc(offer)} · макс {kc(max_price)} · {escape(listing.location or '?')}")
    return "\n".join(lines), summary


def format_gone(title: str, price: int | None, url: str) -> tuple[str, str]:
    text = f"❌ Пропало объявление, которому ты писал:\n<b>{escape(title)}</b> — {kc(price)} Kč\n🔗 {escape(url)}"
    summary = f"❌ пропало: <a href=\"{escape(url)}\">{escape(title)}</a>"
    return text, summary


def split_messages(header: str, lines: list[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Join lines into as few messages as fit Telegram's limit (the header goes into the first)."""
    out, cur = [], header
    for line in lines:
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out
