import asyncio
import logging
import re
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update, User
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

import config
from database import RaceDatabase, RaceResult
from ocr_service import OcrError, recognize_image
from parser import ParsedRaceData, normalize_car_rank, parse_race_text, time_to_seconds

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db = RaceDatabase(config.DATABASE_PATH)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Топ"), KeyboardButton("Помощь")]],
    resize_keyboard=True,
)

def build_top_track_keyboard() -> InlineKeyboardMarkup:
    track_ids = list(config.TRACKS.keys())
    rows = [
        [
            InlineKeyboardButton(config.TRACKS[track_id], callback_data=f"top:{track_id}:all")
            for track_id in track_ids[index : index + 2]
        ]
        for index in range(0, len(track_ids), 2)
    ]
    return InlineKeyboardMarkup(rows)


TOP_TRACK_KEYBOARD = build_top_track_keyboard()


def build_top_keyboard(track: str, car_class: str = "all") -> InlineKeyboardMarkup:
    track_buttons = [
        InlineKeyboardButton(
            config.TRACKS[track_id],
            callback_data=f"top:{track_id}:{car_class}",
        )
        for track_id in config.TRACKS
    ]
    class_ids = list(config.CAR_CLASSES.keys())
    class_row_1 = [
        InlineKeyboardButton(
            config.CAR_CLASSES[class_id],
            callback_data=f"top:{track}:{class_id}",
        )
        for class_id in class_ids[:4]
    ]
    class_row_2 = [
        InlineKeyboardButton(
            config.CAR_CLASSES[class_id],
            callback_data=f"top:{track}:{class_id}",
        )
        for class_id in class_ids[4:]
    ]
    return InlineKeyboardMarkup([track_buttons, class_row_1, class_row_2])


class ResultRejectedError(Exception):
    pass


def validate_result_values(*, time_str: str, time_seconds: float | None, max_speed: str, max_speed_value: float | None) -> None:
    if time_seconds is None:
        raise ValueError("Не удалось проверить время заезда")
    if time_seconds < config.MIN_LAP_TIME_SECONDS:
        raise ResultRejectedError(
            f'Результат отклонён: "{time_str}"\n'
            "Такой заезд не может быть засчитан."
        )
    if max_speed_value is None:
        raise ValueError("Не удалось проверить максимальную скорость")
    if max_speed_value > config.MAX_LAP_SPEED:
        raise ResultRejectedError(
            f'Результат отклонён: "{max_speed} км/ч"\n'
            "Такой заезд не может быть засчитан."
        )


def validate_result(parsed: ParsedRaceData) -> None:
    validate_result_values(
        time_str=parsed.time,
        time_seconds=parsed.time_seconds,
        max_speed=parsed.max_speed,
        max_speed_value=parsed.max_speed_value,
    )


FIELD_EDIT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("track", re.compile(r"^(?:трасса)\s*[:：]\s*(.+)$", re.IGNORECASE)),
    ("car_rank", re.compile(r"^(?:ранг\s*авто|ранг)\s*[:：]\s*(.+)$", re.IGNORECASE)),
    ("car", re.compile(r"^(?:авто|машина)\s*[:：]\s*(.+)$", re.IGNORECASE)),
    ("engine", re.compile(r"^(?:мотор|двигатель)\s*[:：]\s*(.+)$", re.IGNORECASE)),
    ("time", re.compile(r"^(?:время)\s*[:：]\s*(.+)$", re.IGNORECASE)),
    ("max_speed", re.compile(r"^(?:макс\.?\s*скорость|скорость)\s*[:：]\s*(.+)$", re.IGNORECASE)),
]


def format_result(data: ParsedRaceData) -> str:
    return (
        f"Трасса: {data.track_name}\n"
        f"Ранг авто: {data.car_rank}\n"
        f"Авто: {data.car}\n"
        f"Мотор: {data.engine}\n"
        f"Время: {data.time}\n"
        f"Макс. скорость: {data.max_speed}"
    )


def get_user_info(user: User | None) -> tuple[int, str | None, str]:
    if not user:
        return 0, None, "Неизвестный"

    display_name = " ".join(part for part in (user.first_name, user.last_name) if part)
    if not display_name and user.username:
        display_name = f"@{user.username}"
    if not display_name:
        display_name = f"ID {user.id}"

    return user.id, user.username, display_name


def format_user_link(item) -> str:
    display_name = escape(item.user_name or f"ID {item.user_id}")
    if item.username:
        url = f"https://t.me/{item.username}"
    else:
        url = f"tg://user?id={item.user_id}"
    status_icon = "✅" if item.confirmed else "❌"
    return f'<a href="{url}">{display_name}</a> {status_icon}'


def format_saved_result(item, track_name: str) -> str:
    return (
        f"Трасса: {track_name}\n"
        f"Игрок: {item.user_name}\n"
        f"Ранг авто: {item.car_rank}\n"
        f"Авто: {item.car}\n"
        f"Мотор: {item.engine}\n"
        f"Время: {item.time}\n"
        f"Макс. скорость: {item.max_speed}"
    )


def format_result_from_db(result: RaceResult, track_name: str) -> str:
    return (
        f"Трасса: {track_name}\n"
        f"Ранг авто: {result.car_rank}\n"
        f"Авто: {result.car}\n"
        f"Мотор: {result.engine}\n"
        f"Время: {result.time}\n"
        f"Макс. скорость: {result.max_speed}"
    )


def parse_admin_edits(text: str) -> dict[str, str]:
    edits: dict[str, str] = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        for field, pattern in FIELD_EDIT_PATTERNS:
            match = pattern.match(line)
            if match:
                edits[field] = match.group(1).strip()
                break
    return edits


def resolve_track_id(name: str) -> str | None:
    normalized = name.strip().lower()
    for track_id, display_name in config.TRACKS.items():
        if track_id.lower() == normalized or display_name.lower() == normalized:
            return track_id
    return None


def parse_speed_value(raw: str) -> tuple[str, float]:
    match = re.search(r"(\d+(?:[.,]\d+)?)", raw)
    if not match:
        raise ValueError(f"Не удалось распознать скорость: {raw}")
    number = float(match.group(1).replace(",", "."))
    display = raw if re.search(r"km/h|км/ч", raw, re.IGNORECASE) else f"{int(number) if number.is_integer() else number} km/h"
    return display, number


def review_key(track: str, result_id: int) -> str:
    return f"{track}:{result_id}"


def register_review_notification(
    context: ContextTypes.DEFAULT_TYPE,
    track: str,
    result_id: int,
    admin_id: int,
    message_id: int,
) -> None:
    store_review_message(context, admin_id, message_id, track, result_id)
    reviews = context.bot_data.setdefault("pending_reviews", {})
    key = review_key(track, result_id)
    if key not in reviews:
        reviews[key] = {"status": "pending", "messages": []}
    reviews[key]["messages"].append((admin_id, message_id))


def get_review_status(context: ContextTypes.DEFAULT_TYPE, track: str, result_id: int) -> str | None:
    review = context.bot_data.get("pending_reviews", {}).get(review_key(track, result_id))
    return review["status"] if review else None


def set_review_status(context: ContextTypes.DEFAULT_TYPE, track: str, result_id: int, status: str) -> None:
    reviews = context.bot_data.setdefault("pending_reviews", {})
    key = review_key(track, result_id)
    if key in reviews:
        reviews[key]["status"] = status


async def close_review_for_all_admins(
    context: ContextTypes.DEFAULT_TYPE,
    track: str,
    result_id: int,
    text: str,
) -> None:
    review = context.bot_data.get("pending_reviews", {}).get(review_key(track, result_id))
    if not review:
        return

    for admin_id, message_id in review["messages"]:
        try:
            await context.bot.edit_message_caption(
                chat_id=admin_id,
                message_id=message_id,
                caption=text,
                reply_markup=None,
            )
        except Exception:
            try:
                await context.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=None,
                )
            except Exception:
                logger.exception("Failed to close review message for admin %s", admin_id)


def store_review_message(
    context: ContextTypes.DEFAULT_TYPE,
    admin_id: int,
    message_id: int,
    track: str,
    result_id: int,
) -> None:
    context.bot_data.setdefault("review_messages", {})[(admin_id, message_id)] = (track, result_id)


def get_review_message(context: ContextTypes.DEFAULT_TYPE, admin_id: int, message_id: int) -> tuple[str, int] | None:
    return context.bot_data.get("review_messages", {}).get((admin_id, message_id))


def build_admin_review_caption(user_name: str, result: RaceResult, track_name: str) -> str:
    return (
        f"Новый результат от {escape(user_name)}\n\n"
        f"{escape(format_result_from_db(result, track_name))}\n\n"
        "↩️ Ответьте на это сообщение с исправлением, например:\n"
        "<code>Мотор: Z513</code>"
    )


async def update_admin_review_message(query, text: str) -> None:
    if not query.message:
        return
    if query.message.caption is not None:
        await query.message.edit_caption(caption=text, reply_markup=None)
    else:
        await query.message.edit_text(text, reply_markup=None)


def build_admin_review_keyboard(track: str, result_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{track}:{result_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{track}:{result_id}"),
            ]
        ]
    )


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in config.ADMIN_IDS


async def notify_admins_for_review(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    parsed: ParsedRaceData,
    result_id: int,
    user_name: str,
    photo_file_id: str | None = None,
    document_file_id: str | None = None,
) -> None:
    if not config.ADMIN_IDS:
        logger.warning("ADMIN_IDS не задан — результат сохранён без отправки на проверку")
        return

    caption = (
        f"Новый результат от {escape(user_name)}\n\n"
        f"{escape(format_result(parsed))}\n\n"
        "↩️ Ответьте на это сообщение с исправлением, например:\n"
        "<code>Мотор: Z513</code>"
    )
    keyboard = build_admin_review_keyboard(parsed.track, result_id)

    for admin_id in config.ADMIN_IDS:
        try:
            if photo_file_id:
                message = await context.bot.send_photo(
                    admin_id,
                    photo_file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            elif document_file_id:
                message = await context.bot.send_document(
                    admin_id,
                    document_file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            else:
                message = await context.bot.send_message(
                    admin_id,
                    caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            register_review_notification(context, parsed.track, result_id, admin_id, message.message_id)
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)


async def save_pending_result(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    parsed: ParsedRaceData,
    user_id: int,
    username: str | None,
    user_name: str,
    photo_file_id: str | None = None,
    document_file_id: str | None = None,
) -> str:
    result_id = db.add_result(
        track=parsed.track,
        car_rank=parsed.car_rank,
        car=parsed.car,
        engine=parsed.engine,
        time=parsed.time,
        time_seconds=parsed.time_seconds,
        max_speed=parsed.max_speed,
        max_speed_value=parsed.max_speed_value,
        user_id=user_id,
        username=username,
        user_name=user_name,
    )

    await notify_admins_for_review(
        context,
        parsed=parsed,
        result_id=result_id,
        user_name=user_name,
        photo_file_id=photo_file_id,
        document_file_id=document_file_id,
    )

    return (
        "⏳ Результат отправлен на проверку администратору:\n\n"
        f"{format_result(parsed)}"
    )


def format_top_results(results, track_name: str, car_class: str = "all") -> str:
    class_label = config.CAR_CLASSES.get(car_class, car_class)
    if not results:
        return (
            f"Пока нет записей на трассе «{track_name}» "
            f"(класс {class_label}). Отправьте скриншот гонки, чтобы добавить результат."
        )

    lines = [f"🏁 Топ-10 — {track_name} — класс {class_label}:\n"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for index, item in enumerate(results, start=1):
        place = medals.get(index, f"{index}.")
        lines.append(
            f"{place} {item.car} | {item.engine}\n"
            f"   Ранг: {item.car_rank} | Время: {item.time} | Скорость: {item.max_speed}\n"
            f"   Игрок: {format_user_link(item)}"
        )
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Отправьте скриншот экрана результатов гонки — я все распознаю и запишу ваш результат!\n"
        "/top — показать топ по трассам\n"
        "/help — справка",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_question"] = True
    await update.message.reply_text(
        "Отправьте фото или скриншот с результатами гонки.\n"
        "Бот определяет трассу и ищет данные: "
        "ранг авто, авто, мотор, время, максимальная скорость.\n\n"
        "Лучшие результаты сортируются по времени (меньше — лучше).\n\n"
        "Если нужна помощь — напишите свой вопрос следующим сообщением, "
        "и администратор ответит вам.",
        reply_markup=MAIN_KEYBOARD,
    )


def store_support_message(context: ContextTypes.DEFAULT_TYPE, admin_id: int, message_id: int, user_id: int) -> None:
    context.bot_data.setdefault("support_replies", {})[(admin_id, message_id)] = user_id


async def notify_admins_about_question(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user_id: int,
    user_name: str,
    question: str,
) -> bool:
    if not config.ADMIN_IDS:
        return False

    text = (
        f"❓ Вопрос от {escape(user_name)} (ID: {user_id}):\n\n"
        f"{escape(question)}\n\n"
        "↩️ Ответьте на это сообщение, чтобы отправить ответ пользователю."
    )

    sent = False
    for admin_id in config.ADMIN_IDS:
        try:
            message = await context.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
            store_support_message(context, admin_id, message.message_id, user_id)
            sent = True
        except Exception:
            logger.exception("Failed to send support question to admin %s", admin_id)
    return sent


async def handle_user_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text or not message.from_user:
        return
    if not context.user_data.get("awaiting_question"):
        return

    context.user_data["awaiting_question"] = False
    user_id, _, user_name = get_user_info(message.from_user)
    question = message.text.strip()
    if not question:
        await message.reply_text("Введите текст вопроса.", reply_markup=MAIN_KEYBOARD)
        context.user_data["awaiting_question"] = True
        return

    if await notify_admins_about_question(
        context,
        user_id=user_id,
        user_name=user_name,
        question=question,
    ):
        await message.reply_text(
            "✅ Ваш вопрос отправлен администратору. Ожидайте ответ.",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await message.reply_text(
            "Сейчас администратор недоступен. Попробуйте позже.",
            reply_markup=MAIN_KEYBOARD,
        )


async def apply_admin_edits(track: str, result_id: int, edits: dict[str, str]) -> tuple[str, int, RaceResult]:
    result = db.get_result(track, result_id)
    if not result:
        raise ValueError("Результат не найден")

    if "track" in edits:
        new_track = resolve_track_id(edits["track"])
        if not new_track:
            raise ValueError(f"Неизвестная трасса: {edits['track']}")
        moved_id = db.move_result(track, result_id, new_track)
        if moved_id is None:
            raise ValueError("Не удалось перенести результат на другую трассу")
        track = new_track
        result_id = moved_id
        result = db.get_result(track, result_id)
        if not result:
            raise ValueError("Результат не найден после переноса")

    update_kwargs: dict[str, object] = {}
    if "car_rank" in edits:
        update_kwargs["car_rank"] = normalize_car_rank(edits["car_rank"])
    if "car" in edits:
        update_kwargs["car"] = edits["car"]
    if "engine" in edits:
        update_kwargs["engine"] = edits["engine"]
    if "time" in edits:
        time_str = edits["time"].replace(",", ".")
        time_seconds = time_to_seconds(time_str)
        update_kwargs["time"] = time_str
        update_kwargs["time_seconds"] = time_seconds
    if "max_speed" in edits:
        max_speed, max_speed_value = parse_speed_value(edits["max_speed"])
        update_kwargs["max_speed"] = max_speed
        update_kwargs["max_speed_value"] = max_speed_value

    if update_kwargs:
        db.update_result(track, result_id, **update_kwargs)

    result = db.get_result(track, result_id)
    if not result:
        raise ValueError("Результат не найден")

    validate_result_values(
        time_str=result.time,
        time_seconds=result.time_seconds,
        max_speed=result.max_speed,
        max_speed_value=result.max_speed_value,
    )
    return track, result_id, result


async def update_review_notification(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    message_id: int,
    user_name: str,
    track: str,
    result_id: int,
    result: RaceResult,
) -> None:
    track_name = config.TRACKS[track]
    caption = build_admin_review_caption(user_name, result, track_name)
    keyboard = build_admin_review_keyboard(track, result_id)
    store_review_message(context, chat_id, message_id, track, result_id)

    try:
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def handle_admin_result_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if not message or not message.reply_to_message or not message.text or not message.from_user:
        return False
    if not is_admin(message.from_user.id):
        return False

    review_key = (message.chat_id, message.reply_to_message.message_id)
    review_info = get_review_message(context, message.chat_id, message.reply_to_message.message_id)
    if not review_info:
        return False

    track, result_id = review_info
    edits = parse_admin_edits(message.text)
    if not edits:
        await message.reply_text(
            "Не удалось распознать исправление.\n"
            "Пример: <code>Мотор: Z513</code>",
            parse_mode=ParseMode.HTML,
        )
        return True

    try:
        track, result_id, result = await apply_admin_edits(track, result_id, edits)
    except ResultRejectedError as exc:
        await message.reply_text(str(exc))
        return True
    except ValueError as exc:
        await message.reply_text(str(exc))
        return True

    user_name = result.user_name or f"ID {result.user_id}"
    try:
        await update_review_notification(
            context,
            chat_id=message.chat_id,
            message_id=message.reply_to_message.message_id,
            user_name=user_name,
            track=track,
            result_id=result_id,
            result=result,
        )
    except Exception:
        logger.exception("Failed to update review notification")

    await message.reply_text(
        "✅ Результат обновлён:\n\n"
        f"{format_saved_result(result, config.TRACKS[track])}"
    )
    return True


async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await handle_admin_result_edit(update, context):
        return

    message = update.message
    if not message or not message.reply_to_message or not message.text or not message.from_user:
        return
    if not is_admin(message.from_user.id):
        return

    key = (message.chat_id, message.reply_to_message.message_id)
    user_id = context.bot_data.get("support_replies", {}).get(key)
    if not user_id:
        return

    try:
        await context.bot.send_message(
            user_id,
            f"💬 Ответ администратора:\n\n{message.text}",
        )
        await message.reply_text("✅ Ответ отправлен пользователю.")
    except Exception:
        logger.exception("Failed to send admin reply to user %s", user_id)
        await message.reply_text("Не удалось отправить ответ пользователю.")


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message or (update.callback_query.message if update.callback_query else None)
    if not message:
        return

    await message.reply_text(
        "Выберите трассу:",
        reply_markup=TOP_TRACK_KEYBOARD,
    )


async def admin_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return

    if not is_admin(query.from_user.id):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    action, track, result_id_raw = query.data.split(":", 2)
    if track not in config.TRACKS or not result_id_raw.isdigit():
        await query.answer("Некорректные данные")
        return

    result_id = int(result_id_raw)
    review_status = get_review_status(context, track, result_id)
    if review_status in {"confirmed", "rejected"}:
        message = "Результат уже подтверждён" if review_status == "confirmed" else "Результат уже обработан"
        await query.answer(message, show_alert=True)
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
        return

    result = db.get_result(track, result_id)
    if not result:
        await query.answer("Результат уже обработан", show_alert=True)
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
        return

    track_name = config.TRACKS[track]

    if action == "confirm":
        if result.confirmed:
            await query.answer("Результат уже подтверждён", show_alert=True)
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
            return

        if not db.confirm_result_if_pending(track, result_id):
            set_review_status(context, track, result_id, "confirmed")
            await query.answer("Результат уже подтверждён", show_alert=True)
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
            return

        set_review_status(context, track, result_id, "confirmed")
        status_text = f"✅ Подтверждено\n\n{format_saved_result(result, track_name)}"
        await close_review_for_all_admins(context, track, result_id, status_text)
        await query.answer("Результат подтверждён")
        try:
            await context.bot.send_message(
                result.user_id,
                f"✅ Ваш результат на трассе «{track_name}» подтверждён администратором.\n\n"
                f"Авто: {result.car}\n"
                f"Время: {result.time}",
            )
        except Exception:
            logger.exception("Failed to notify user %s about confirmation", result.user_id)
        return

    if action == "reject":
        if result.confirmed:
            await query.answer("Результат уже подтверждён", show_alert=True)
            if query.message:
                await query.message.edit_reply_markup(reply_markup=None)
            return

        db.delete_result(track, result_id)
        set_review_status(context, track, result_id, "rejected")
        status_text = f"❌ Отклонено\n\n{format_saved_result(result, track_name)}"
        await close_review_for_all_admins(context, track, result_id, status_text)
        await query.answer("Результат отклонён")
        try:
            await context.bot.send_message(
                result.user_id,
                f"❌ Ваш результат на трассе «{track_name}» отклонён администратором.\n\n"
                f"Авто: {result.car}\n"
                f"Время: {result.time}",
            )
        except Exception:
            logger.exception("Failed to notify user %s about rejection", result.user_id)


async def top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("top:"):
        return

    parts = query.data.split(":")
    if len(parts) == 2:
        _, track = parts
        car_class = "all"
    elif len(parts) == 3:
        _, track, car_class = parts
    else:
        await query.answer("Некорректные данные")
        return

    if track not in config.TRACKS:
        await query.answer("Неизвестная трасса")
        return
    if car_class not in config.CAR_CLASSES:
        await query.answer("Неизвестный класс")
        return

    await query.answer()
    track_name = config.TRACKS[track]
    results = db.get_top_results(track, limit=10, car_class=car_class)
    await query.edit_message_text(
        format_top_results(results, track_name, car_class),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=build_top_keyboard(track, car_class),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.photo:
        return

    status_message = await message.reply_text("Обрабатываю скриншот...")

    try:
        photo = message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        text = recognize_image(bytes(image_bytes), config.OCR_SPACE_API_KEY)
        parsed = parse_race_text(text)
        validate_result(parsed)

        user_id, username, user_name = get_user_info(message.from_user)

        response_text = await save_pending_result(
            context,
            parsed=parsed,
            user_id=user_id,
            username=username,
            user_name=user_name,
            photo_file_id=photo.file_id,
        )

        await status_message.edit_text(response_text)
    except OcrError as exc:
        await status_message.edit_text(f"Ошибка OCR: {exc}")
    except ResultRejectedError as exc:
        await status_message.edit_text(str(exc))
    except ValueError as exc:
        await status_message.edit_text(
            f"Не удалось разобрать данные гонки: {exc}\n\n"
            "Попробуйте отправить более чёткий скриншот."
        )
    except Exception:
        logger.exception("Failed to process screenshot")
        await status_message.edit_text("Произошла ошибка при обработке скриншота.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.document:
        return

    mime = message.document.mime_type or ""
    if not mime.startswith("image/"):
        await message.reply_text("Отправьте изображение со скриншотом гонки.")
        return

    status_message = await message.reply_text("Обрабатываю скриншот...")

    try:
        file = await context.bot.get_file(message.document.file_id)
        image_bytes = await file.download_as_bytearray()

        text = recognize_image(bytes(image_bytes), config.OCR_SPACE_API_KEY)
        parsed = parse_race_text(text)
        validate_result(parsed)

        user_id, username, user_name = get_user_info(message.from_user)

        response_text = await save_pending_result(
            context,
            parsed=parsed,
            user_id=user_id,
            username=username,
            user_name=user_name,
            document_file_id=message.document.file_id,
        )

        await status_message.edit_text(response_text)
    except OcrError as exc:
        await status_message.edit_text(f"Ошибка OCR: {exc}")
    except ResultRejectedError as exc:
        await status_message.edit_text(str(exc))
    except ValueError as exc:
        await status_message.edit_text(
            f"Не удалось разобрать данные гонки: {exc}\n\n"
            "Попробуйте отправить более чёткий скриншот."
        )
    except Exception:
        logger.exception("Failed to process document image")
        await status_message.edit_text("Произошла ошибка при обработке скриншота.")


def main() -> None:
    request = HTTPXRequest(httpx_kwargs={"trust_env": False})
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).request(request).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(CallbackQueryHandler(top_callback, pattern=r"^top:"))
    application.add_handler(CallbackQueryHandler(admin_review_callback, pattern=r"^(confirm|reject):"))
    application.add_handler(MessageHandler(filters.Regex("^Топ$"), top_command))
    application.add_handler(MessageHandler(filters.Regex("^Помощь$"), help_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    application.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_admin_reply))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.Regex("^(Топ|Помощь)$"),
            handle_user_question,
        )
    )

    logger.info("Bot started")
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
