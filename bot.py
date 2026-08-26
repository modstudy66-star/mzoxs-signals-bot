"""بوت MZOXS Signals لإدارة العروض والاشتراكات المدفوعة بنجوم تلغرام.

يستخدم البوت long polling ومكتبة python-telegram-bot الحديثة، ويتولى:
- بيع اشتراك 30 يوماً في قناة خاصة عبر Telegram Stars.
- إرسال رابط دخول فردي للقناة الخاصة بعد نجاح الدفع.
- استقبال عروض الإدارة (صورة ثم نص ثم رابط) ونشرها في القناتين.
- إرسال عروض مجدولة للمستخدمين، وحفظ كل البيانات في SQLite.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from database import Database


# لا تسجل هذه الإعدادات التوكن أو أي قيمة من ملف البيئة.
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INVOICE_PAYLOAD = "mzoxs_vip_month_30_days_v2"
STARS_CURRENCY = "XTR"
SUBSCRIPTION_DAYS = 30
MAX_OFFER_TEXT_LENGTH = 900  # حد آمن لشرح صورة تلغرام (caption).


@dataclass(frozen=True)
class Settings:
    """إعدادات البوت المحملة من متغيرات البيئة فقط."""

    bot_token: str
    admin_ids: frozenset[int]
    free_channel_id: str
    private_channel_id: int
    private_channel_name: str
    free_channel_url: str
    contact_url: str
    database_path: str
    timezone: ZoneInfo
    monthly_price_stars: int


def _parse_admin_ids(raw_value: str) -> frozenset[int]:
    """يحول ADMIN_IDS المفصولة بفاصلة إلى أرقام حسابات تلغرام."""
    ids: set[int] = set()
    for item in raw_value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise RuntimeError("ADMIN_IDS يجب أن يحتوي أرقام Telegram ID فقط.") from exc
    return frozenset(ids)


def _parse_private_channel_id(raw_value: str) -> int:
    """يتحقق من المعرف الرقمي للقناة الخاصة."""
    try:
        return int(raw_value.strip())
    except ValueError as exc:
        raise RuntimeError(
            "ضع PRIVATE_CHANNEL_ID الرقمي للقناة الخاصة في ملف .env."
        ) from exc


def load_settings() -> Settings:
    """يحمّل الإعدادات ويتحقق من اكتمالها قبل تشغيل البوت."""
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token or bot_token.startswith("ضع_"):
        raise RuntimeError("ضع BOT_TOKEN الصحيح في ملف .env أو متغيرات Railway.")

    price_raw = os.getenv("MONTHLY_PRICE_STARS", "5500").strip()
    try:
        monthly_price_stars = int(price_raw)
    except ValueError as exc:
        raise RuntimeError("MONTHLY_PRICE_STARS يجب أن يكون رقماً صحيحاً.") from exc
    if monthly_price_stars <= 0:
        raise RuntimeError("MONTHLY_PRICE_STARS يجب أن يكون أكبر من صفر.")

    timezone_name = os.getenv("TIMEZONE", "Asia/Baghdad").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise RuntimeError("قيمة TIMEZONE غير صحيحة. مثال: Asia/Baghdad") from exc

    return Settings(
        bot_token=bot_token,
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS", "")),
        # يقبل اسم المستخدم العام أو المعرف -100... للقناة المجانية.
        free_channel_id=os.getenv("FREE_CHANNEL_ID", "@MZOXS_SIGNALS_FREE").strip(),
        private_channel_id=_parse_private_channel_id(
            os.getenv("PRIVATE_CHANNEL_ID", "")
        ),
        private_channel_name=os.getenv(
            "PRIVATE_CHANNEL_NAME", "MZOXS Signals VIP"
        ).strip(),
        free_channel_url=os.getenv(
            "FREE_CHANNEL_URL", "https://t.me/MZOXS_SIGNALS_FREE"
        ).strip(),
        contact_url=os.getenv("CONTACT_URL", "https://t.me/your_username").strip(),
        database_path=os.getenv("DATABASE_PATH", "data/bot.db").strip(),
        timezone=timezone,
        monthly_price_stars=monthly_price_stars,
    )


def get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    """يعيد كائن SQLite المربوط بالتطبيق."""
    return context.application.bot_data["db"]


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    """يعيد إعدادات التطبيق المقروءة عند الإقلاع."""
    return context.application.bot_data["settings"]


def is_admin(user_id: int | None, settings: Settings) -> bool:
    """يتحقق من المدير عبر Telegram ID الثابت وليس اسم المستخدم المتغير."""
    return user_id is not None and user_id in settings.admin_ids


def is_valid_url(value: str) -> bool:
    """يسمح فقط بروابط HTTP وHTTPS الصالحة لزر العرض."""
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def main_menu() -> InlineKeyboardMarkup:
    """لوحة المستخدم الرئيسية المطلوبة."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("العروض اليومية", callback_data="menu:offers")],
            [InlineKeyboardButton("القناة الخاصة", callback_data="menu:vip")],
            [InlineKeyboardButton("تواصل معنا", callback_data="menu:contact")],
        ]
    )


def back_to_menu_button() -> InlineKeyboardMarkup:
    """زر رجوع موحد إلى قائمة البداية."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("العودة إلى القائمة", callback_data="menu:home")]]
    )


def offers_keyboard(free_channel_url: str) -> InlineKeyboardMarkup:
    """أزرار شاشة العروض مع رابط القناة المجانية."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("قناة التوصيات المجانية", url=free_channel_url)],
            [InlineKeyboardButton("العودة إلى القائمة", callback_data="menu:home")],
        ]
    )


def subscribe_keyboard() -> InlineKeyboardMarkup:
    """أزرار عملية شراء الاشتراك الشهري."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("شراء اشتراك شهر واحد", callback_data="pay:vip")],
            [InlineKeyboardButton("العودة إلى القائمة", callback_data="menu:home")],
        ]
    )


def admin_menu() -> InlineKeyboardMarkup:
    """لوحة الإدارة داخل تلغرام."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("إضافة عرض مصور", callback_data="admin:add")],
            [InlineKeyboardButton("قائمة العروض", callback_data="admin:list")],
            [InlineKeyboardButton("حذف عرض", callback_data="admin:delete")],
            [InlineKeyboardButton("إحصاءات البوت", callback_data="admin:stats")],
        ]
    )


def offer_keyboard(offer) -> InlineKeyboardMarkup | None:
    """ينشئ زر الرابط للعرض إذا كان الرابط محفوظاً."""
    if not offer["button_url"]:
        return None
    button_text = offer["button_text"] or "فتح العرض"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(button_text, url=offer["button_url"])]]
    )


def render_offers(offers: list, *, title: str = "العروض اليومية") -> str:
    """يعرض قائمة نصية مختصرة للعروض المخزنة."""
    if not offers:
        return f"<b>{html.escape(title)}</b>\n\nلا توجد عروض منشورة حالياً. راجعنا قريباً."

    lines = [f"<b>{html.escape(title)}</b>", ""]
    for index, offer in enumerate(offers, start=1):
        lines.append(f"<b>{index}.</b> {html.escape(offer['text'])}")
        if offer["button_url"]:
            lines.append(f"الرابط: {html.escape(offer['button_url'])}")
        lines.append("")
    return "\n".join(lines).strip()


def format_stats(stats: dict[str, int]) -> str:
    """يبني نصاً موحداً لأمر /stats ولوحة الإدارة."""
    return (
        "<b>إحصاءات البوت</b>\n\n"
        f"إجمالي المستخدمين: <b>{stats['total_users']}</b>\n"
        f"المستخدمون المتاحون للإرسال: <b>{stats['active_users']}</b>\n"
        f"المشتركون النشطون: <b>{stats['active_subscribers']}</b>\n"
        f"العروض النشطة: <b>{stats['active_offers']}</b>\n"
        f"عدد الدفعات المسجلة: <b>{stats['payments_count']}</b>\n"
        f"إجمالي النجوم المسجلة: <b>{stats['stars_total']}</b>"
    )


async def upsert_current_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يسجل المستخدم الذي بدأ أو تفاعل مع البوت."""
    if not update.effective_user:
        return
    user = update.effective_user
    get_db(context).upsert_user(user.id, user.username, user.full_name)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض قائمة البداية."""
    await upsert_current_user(update, context)
    if not update.effective_message:
        return
    name = html.escape(update.effective_user.first_name if update.effective_user else "بك")
    await update.effective_message.reply_text(
        f"أهلاً {name}،\n\nاختر القسم الذي تريد الوصول إليه:",
        reply_markup=main_menu(),
    )


async def show_home(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعدل رسالة الأزرار الحالية ليعرض الصفحة الرئيسية."""
    name = html.escape(query.from_user.first_name)
    await query.edit_message_text(
        f"أهلاً {name}،\n\nاختر القسم الذي تريد الوصول إليه:",
        reply_markup=main_menu(),
    )


async def show_offers(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض أحدث العروض المحفوظة مع رابط القناة المجانية."""
    offers = get_db(context).get_active_offers(limit=20)
    await query.edit_message_text(
        render_offers(offers),
        reply_markup=offers_keyboard(get_settings(context).free_channel_url),
    )


async def create_private_invite_link(
    user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> str:
    """ينشئ رابط دخول فردياً مدته ساعة للقناة المدفوعة.

    يحتاج البوت إلى صلاحية دعوة المستخدمين في القناة الخاصة.
    """
    settings = get_settings(context)
    expires_at = datetime.now(settings.timezone) + timedelta(hours=1)
    invite = await context.bot.create_chat_invite_link(
        chat_id=settings.private_channel_id,
        name=f"VIP-{user_id}-{int(expires_at.timestamp())}",
        expire_date=expires_at,
        member_limit=1,
    )
    return invite.invite_link


async def show_vip(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض حالة الاشتراك أو يدعو المستخدم للشراء."""
    db = get_db(context)
    settings = get_settings(context)
    user_id = query.from_user.id

    if db.has_active_subscription(user_id):
        expires_at = db.get_subscription_until(user_id)
        try:
            invite_link = await create_private_invite_link(user_id, context)
        except TelegramError:
            logger.exception("تعذر إنشاء رابط دعوة خاص للقناة")
            await query.edit_message_text(
                "اشتراكك نشط، لكن تعذر إنشاء رابط الدخول الآن. تواصل مع الإدارة.",
                reply_markup=back_to_menu_button(),
            )
            return

        await query.edit_message_text(
            "<b>اشتراكك نشط</b>\n\n"
            f"القناة: <b>{html.escape(settings.private_channel_name)}</b>\n"
            f"ينتهي اشتراكك في: <b>{expires_at.astimezone(settings.timezone).strftime('%Y-%m-%d %H:%M')}</b>\n\n"
            "هذا رابط فردي صالح لمدة ساعة ولمستخدم واحد فقط:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("دخول القناة الخاصة", url=invite_link)],
                    [InlineKeyboardButton("العودة إلى القائمة", callback_data="menu:home")],
                ]
            ),
        )
        return

    await query.edit_message_text(
        f"<b>{html.escape(settings.private_channel_name)}</b>\n\n"
        f"اشترك لمدة <b>{SUBSCRIPTION_DAYS} يوماً</b> مقابل "
        f"<b>{settings.monthly_price_stars} نجمة</b>.\n\n"
        "بعد تأكيد الدفع يرسل البوت رابط دخول فردياً إلى القناة تلقائياً.",
        reply_markup=subscribe_keyboard(),
    )


async def show_contact(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يوجه المستخدم إلى رابط التواصل المضبوط في البيئة."""
    settings = get_settings(context)
    await query.edit_message_text(
        "يمكنك التواصل معنا مباشرة عبر الزر التالي:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("فتح المحادثة", url=settings.contact_url)],
                [InlineKeyboardButton("العودة إلى القائمة", callback_data="menu:home")],
            ]
        ),
    )


async def send_stars_invoice(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل فاتورة الاشتراك باستخدام عملة Telegram Stars (XTR)."""
    settings = get_settings(context)
    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title=f"اشتراك {settings.private_channel_name}",
        description=f"دخول إلى القناة الخاصة لمدة {SUBSCRIPTION_DAYS} يوماً.",
        payload=INVOICE_PAYLOAD,
        # فواتير Telegram Stars لا تستخدم provider_token.
        provider_token="",
        currency=STARS_CURRENCY,
        prices=[LabeledPrice("اشتراك شهر واحد", settings.monthly_price_stars)],
        start_parameter="mzoxs-vip-month",
        protect_content=True,
    )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يتحقق من المنتج والمبلغ قبل تأكيد الدفع في تلغرام."""
    query = update.pre_checkout_query
    if query is None:
        return

    settings = get_settings(context)
    valid = (
        query.invoice_payload == INVOICE_PAYLOAD
        and query.currency == STARS_CURRENCY
        and query.total_amount == settings.monthly_price_stars
    )
    if not valid:
        await query.answer(
            ok=False,
            error_message="تعذر التحقق من طلب الدفع. أعد المحاولة من فضلك.",
        )
        logger.warning("تم رفض عملية تحقق قبل الدفع بسبب بيانات غير مطابقة.")
        return
    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يثبت الدفع ويضيف 30 يوماً ويرسل رابط الدخول الفردي مباشرة."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.successful_payment is None:
        return

    payment = message.successful_payment
    settings = get_settings(context)
    if (
        payment.invoice_payload != INVOICE_PAYLOAD
        or payment.currency != STARS_CURRENCY
        or payment.total_amount != settings.monthly_price_stars
    ):
        logger.error("وصل إشعار دفع لا يطابق منتج الاشتراك المتوقع.")
        await message.reply_text(
            "استلمنا الدفعة لكن نحتاج إلى مراجعتها. تواصل مع الإدارة من فضلك."
        )
        return

    db = get_db(context)
    is_new, expires_at = db.create_payment_and_extend_subscription(
        user_id=user.id,
        telegram_payment_charge_id=payment.telegram_payment_charge_id,
        invoice_payload=payment.invoice_payload,
        currency=payment.currency,
        total_amount=payment.total_amount,
        subscription_days=SUBSCRIPTION_DAYS,
    )
    if not is_new:
        await message.reply_text(
            "هذه الدفعة مسجلة مسبقاً، واشتراكك ما زال فعالاً.",
            reply_markup=main_menu(),
        )
        return

    try:
        invite_link = await create_private_invite_link(user.id, context)
    except TelegramError:
        logger.exception("تم الدفع لكن تعذر إنشاء رابط دعوة القناة")
        await message.reply_text(
            "تم تأكيد دفعك بنجاح، لكن تعذر إنشاء رابط الدخول الآن. "
            "تواصل مع الإدارة وسيتم مساعدتك فوراً.",
            reply_markup=main_menu(),
        )
        return

    local_expiry = expires_at.astimezone(settings.timezone).strftime("%Y-%m-%d %H:%M")
    await message.reply_text(
        "<b>تم تأكيد الدفع بنجاح</b>\n\n"
        f"القناة: <b>{html.escape(settings.private_channel_name)}</b>\n"
        f"اشتراكك صالح حتى: <b>{local_expiry}</b>\n\n"
        "استخدم الرابط الفردي التالي للدخول. الرابط صالح لمدة ساعة ولمستخدم واحد فقط.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("دخول القناة الخاصة", url=invite_link)],
                [InlineKeyboardButton("العودة إلى القائمة", callback_data="menu:home")],
            ]
        ),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يفتح لوحة الإدارة للمشرفين المخولين فقط."""
    await upsert_current_user(update, context)
    if not update.effective_message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id, get_settings(context)):
        await update.effective_message.reply_text("هذا الأمر مخصص للإدارة فقط.")
        return
    context.user_data.pop("admin_waiting_for", None)
    context.user_data.pop("new_offer", None)
    await update.effective_message.reply_text(
        "<b>لوحة الإدارة</b>\n\nاختر العملية المطلوبة:",
        reply_markup=admin_menu(),
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض عدد المشتركين وإجمالي نجوم الدفعات للمدير عبر /stats."""
    await upsert_current_user(update, context)
    if not update.effective_message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id, get_settings(context)):
        await update.effective_message.reply_text("هذا الأمر مخصص للإدارة فقط.")
        return
    await update.effective_message.reply_text(
        format_stats(get_db(context).get_stats()),
        reply_markup=admin_menu(),
    )


async def admin_stats(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض الإحصاءات من زر الإدارة."""
    await query.edit_message_text(
        format_stats(get_db(context).get_stats()),
        reply_markup=admin_menu(),
    )


async def admin_list_offers(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض قائمة نصية بالعروض النشطة للمدير."""
    offers = get_db(context).get_active_offers(limit=100)
    await query.edit_message_text(
        render_offers(offers, title="قائمة العروض النشطة"),
        reply_markup=admin_menu(),
    )


async def admin_delete_menu(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض أزرار تعطيل العروض المخزنة."""
    offers = get_db(context).get_active_offers(limit=50)
    if not offers:
        await query.edit_message_text("لا توجد عروض لحذفها.", reply_markup=admin_menu())
        return

    buttons = []
    for offer in offers:
        preview = offer["text"].replace("\n", " ").strip()
        buttons.append(
            [
                InlineKeyboardButton(
                    f"حذف #{offer['id']}: {preview[:30]}",
                    callback_data=f"admin:remove:{offer['id']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("العودة للإدارة", callback_data="admin:home")])
    await query.edit_message_text(
        "اختر العرض الذي تريد حذفه. الحذف يوقف إظهاره وإرساله ولا يمسح السجل التاريخي.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_add_offer(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يبدأ المسار المتسلسل: صورة ثم نص ثم رابط."""
    context.user_data["admin_waiting_for"] = "offer_photo"
    context.user_data["new_offer"] = {}
    await query.edit_message_text(
        "<b>إضافة عرض جديد</b>\n\n"
        "أرسل الآن صورة العرض فقط. بعد ذلك سيطلب منك البوت النص ثم الرابط.\n\n"
        "لإلغاء العملية استخدم /cancel.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("إلغاء", callback_data="admin:cancel")]]
        ),
    )


async def publish_offer_to_chat(chat_id: str | int, offer, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ينشر عرضاً كاملاً في محادثة واحدة، مع زر الرابط إن وجد."""
    caption = html.escape(offer["text"])
    keyboard = offer_keyboard(offer)
    if offer["image_file_id"]:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=offer["image_file_id"],
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text=caption, reply_markup=keyboard)


async def publish_offer_to_channels(offer, context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    """ينشر العرض الجديد فور حفظه في القناتين ويعيد نتيجة كل قناة."""
    settings = get_settings(context)
    destinations = [
        ("القناة المجانية", settings.free_channel_id),
        (settings.private_channel_name, settings.private_channel_id),
    ]
    results: list[str] = []
    for label, chat_id in destinations:
        try:
            await publish_offer_to_chat(chat_id, offer, context)
            results.append(f"تم النشر في {label}")
        except TelegramError as exc:
            logger.warning("تعذر نشر العرض في %s: %s", label, exc)
            results.append(f"تعذر النشر في {label}")
    return results


async def admin_receive_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يتلقى عناصر العرض من المدير ويبدأ النشر بعد اكتمالها."""
    if not update.effective_user or not update.effective_message:
        return
    if not is_admin(update.effective_user.id, get_settings(context)):
        return

    stage = context.user_data.get("admin_waiting_for")
    if not stage:
        return
    message = update.effective_message
    new_offer = context.user_data.setdefault("new_offer", {})

    if stage == "offer_photo":
        if not message.photo:
            await message.reply_text("أرسل صورة للعرض أولاً، أو استخدم /cancel للإلغاء.")
            return
        # نستخدم أكبر نسخة توفرها تلغرام لنفس الصورة؛ file_id صالح لإعادة الإرسال.
        new_offer["image_file_id"] = message.photo[-1].file_id
        context.user_data["admin_waiting_for"] = "offer_text"
        await message.reply_text(
            "تم استلام الصورة. أرسل الآن نص العرض (حتى 900 حرف).",
        )
        return

    if stage == "offer_text":
        text = (message.text or "").strip()
        if len(text) < 3:
            await message.reply_text("نص العرض قصير جداً. أرسل نصاً أوضح.")
            return
        if len(text) > MAX_OFFER_TEXT_LENGTH:
            await message.reply_text(
                f"نص العرض أطول من الحد ({MAX_OFFER_TEXT_LENGTH} حرف). اختصره."
            )
            return
        new_offer["text"] = text
        context.user_data["admin_waiting_for"] = "offer_link"
        await message.reply_text(
            "تم استلام النص. أرسل الآن الرابط الكامل للعرض، ويجب أن يبدأ بـ https:// أو http://",
        )
        return

    if stage == "offer_link":
        button_url = (message.text or "").strip()
        if not is_valid_url(button_url):
            await message.reply_text(
                "الرابط غير صالح. أرسل رابطاً كاملاً يبدأ بـ https:// أو http://"
            )
            return

        offer_id = get_db(context).add_offer(
            text=new_offer["text"],
            created_by=update.effective_user.id,
            image_file_id=new_offer["image_file_id"],
            button_text="فتح العرض",
            button_url=button_url,
        )
        offer = get_db(context).get_offer(offer_id)
        context.user_data.pop("admin_waiting_for", None)
        context.user_data.pop("new_offer", None)

        if offer is None:
            await message.reply_text("تعذر قراءة العرض بعد حفظه. حاول مرة أخرى.")
            return
        publish_results = await publish_offer_to_channels(offer, context)
        await message.reply_text(
            f"تم حفظ العرض بنجاح برقم <b>#{offer_id}</b>.\n\n"
            + "\n".join(publish_results),
            reply_markup=admin_menu(),
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يلغي إضافة العرض الحالية للمدير."""
    if not update.effective_user or not update.effective_message:
        return
    if not is_admin(update.effective_user.id, get_settings(context)):
        return
    context.user_data.pop("admin_waiting_for", None)
    context.user_data.pop("new_offer", None)
    await update.effective_message.reply_text("تم إلغاء العملية.", reply_markup=admin_menu())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يوجه أزرار المستخدم والإدارة مع تحقق صلاحيات الإدارة."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    await upsert_current_user(update, context)

    data = query.data
    if data == "menu:home":
        await show_home(query, context)
    elif data == "menu:offers":
        await show_offers(query, context)
    elif data == "menu:vip":
        await show_vip(query, context)
    elif data == "menu:contact":
        await show_contact(query, context)
    elif data == "pay:vip":
        await send_stars_invoice(query, context)
    elif data.startswith("admin:"):
        if not is_admin(query.from_user.id, get_settings(context)):
            await query.answer("غير مصرح لك بهذه العملية.", show_alert=True)
            return
        if data == "admin:home":
            context.user_data.pop("admin_waiting_for", None)
            context.user_data.pop("new_offer", None)
            await query.edit_message_text("<b>لوحة الإدارة</b>", reply_markup=admin_menu())
        elif data == "admin:add":
            await admin_add_offer(query, context)
        elif data == "admin:list":
            await admin_list_offers(query, context)
        elif data == "admin:delete":
            await admin_delete_menu(query, context)
        elif data == "admin:stats":
            await admin_stats(query, context)
        elif data == "admin:cancel":
            context.user_data.pop("admin_waiting_for", None)
            context.user_data.pop("new_offer", None)
            await query.edit_message_text("تم إلغاء العملية.", reply_markup=admin_menu())
        elif data.startswith("admin:remove:"):
            try:
                offer_id = int(data.rsplit(":", maxsplit=1)[1])
            except ValueError:
                await query.answer("رقم العرض غير صحيح.", show_alert=True)
                return
            was_deleted = get_db(context).deactivate_offer(offer_id)
            result = (
                f"تم حذف العرض #{offer_id}."
                if was_deleted
                else "العرض غير موجود أو محذوف مسبقاً."
            )
            await query.edit_message_text(result, reply_markup=admin_menu())


async def send_daily_offer(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل عرضاً بالتناوب للمستخدمين النشطين في أوقات الجدولة الثلاثة."""
    db = get_db(context)
    offer = db.get_next_offer()
    if offer is None:
        logger.info("لم يُرسل عرض مجدول: لا توجد عروض نشطة.")
        return

    user_ids = db.get_active_user_ids()
    if not user_ids:
        logger.info("لم يُرسل العرض المجدول: لا يوجد مستخدمون نشطون.")
        return

    for user_id in user_ids:
        try:
            await publish_offer_to_chat(user_id, offer, context)
            db.record_offer_delivery(offer["id"], user_id, "sent")
        except Forbidden:
            db.set_user_inactive(user_id)
            db.record_offer_delivery(offer["id"], user_id, "blocked")
        except TelegramError as exc:
            logger.warning("فشل إرسال عرض مجدول إلى مستخدم: %s", exc)
            db.record_offer_delivery(offer["id"], user_id, "failed", str(exc)[:300])
        # مهلة خفيفة لاحترام حد الإرسال عند وجود عدد كبير من المستخدمين.
        await asyncio.sleep(0.04)


async def revoke_expired_access(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يسحب عضوية القناة الخاصة من الاشتراكات المنتهية مرة يومياً."""
    db = get_db(context)
    settings = get_settings(context)
    for user_id in db.get_expired_users_pending_revocation():
        try:
            member = await context.bot.get_chat_member(settings.private_channel_id, user_id)
            if member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}:
                # الحظر ثم فك الحظر يخرج العضو مع إبقاء إمكان التجديد لاحقاً.
                await context.bot.ban_chat_member(settings.private_channel_id, user_id)
                await context.bot.unban_chat_member(
                    settings.private_channel_id, user_id, only_if_banned=True
                )
            db.mark_access_revoked(user_id)
            try:
                await context.bot.send_message(
                    user_id,
                    "انتهى اشتراكك في القناة الخاصة. يمكنك التجديد من زر «القناة الخاصة» في /start.",
                )
            except TelegramError:
                pass
        except BadRequest as exc:
            # ربما لم ينضم العضو من الأساس؛ لا نكرر العملية يومياً.
            logger.info("تعذر سحب عضوية مستخدم من القناة: %s", exc)
            db.mark_access_revoked(user_id)
        except TelegramError:
            logger.exception("فشل غير متوقع عند سحب عضوية مستخدم منتهية صلاحيته")


async def post_init(application: Application) -> None:
    """يضبط أوامر واجهة تلغرام ومواعيد مهام البوت الداخلية."""
    settings: Settings = application.bot_data["settings"]
    await application.bot.set_my_commands(
        [
            BotCommand("start", "فتح القائمة الرئيسية"),
            BotCommand("admin", "لوحة الإدارة"),
            BotCommand("stats", "إحصاءات الإدارة"),
            BotCommand("cancel", "إلغاء عملية الإدارة الحالية"),
        ]
    )

    if application.job_queue is None:
        raise RuntimeError("JobQueue غير متاح. ثبّت المكتبة عبر requirements.txt.")

    # المواعيد محلية بحسب TIMEZONE، والقيمة الافتراضية Asia/Baghdad.
    for hour, minute in ((9, 0), (17, 0), (22, 0)):
        application.job_queue.run_daily(
            send_daily_offer,
            time=time(hour=hour, minute=minute, tzinfo=settings.timezone),
            name=f"daily_offer_{hour:02d}_{minute:02d}",
        )
    application.job_queue.run_daily(
        revoke_expired_access,
        time=time(hour=2, minute=5, tzinfo=settings.timezone),
        name="revoke_expired_vip_access",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يسجل الاستثناءات دون كشف تفاصيلها للمستخدمين."""
    logger.exception("حدث استثناء أثناء معالجة تحديث", exc_info=context.error)


def build_application(settings: Settings) -> Application:
    """يبني التطبيق ويربط قاعدة البيانات والمعالجات."""
    database = Database(settings.database_path)
    database.initialize()

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .post_init(post_init)
        .build()
    )
    application.bot_data["db"] = database
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.PHOTO, admin_receive_offer))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_offer))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    """يشغل البوت بـ long polling، المناسب لخدمة Railway المستمرة."""
    settings = load_settings()
    if not settings.admin_ids:
        logger.warning("لم تُضبط ADMIN_IDS؛ لن يستطيع أحد فتح لوحة الإدارة.")
    application = build_application(settings)
    logger.info("بدأ تشغيل البوت. المنطقة الزمنية للجدولة: %s", settings.timezone.key)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
