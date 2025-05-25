import os
import asyncpg
from datetime import datetime, timedelta
from datetime import datetime, date as date_class

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InputMediaPhoto, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import requests
import time
import redis
from io import BytesIO
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Инициализация бота
API_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Глобальная переменная для пула подключений
pool = None


# Состояния FSM
class MemoryStates(StatesGroup):
    waiting_for_date = State()
    waiting_for_place = State()
    waiting_for_rating = State()
    waiting_for_description = State()
    waiting_for_photo = State()
    waiting_for_start_date = State()
    waiting_for_end_date = State()
    waiting_for_custom_date = State()


# Глобальные переменные для пагинации
current_event_index = {}
current_memory_index = {}

# Глобальные переменные для кэша
events_cache = {}
memories_cache = {}


# Подключение к Redis
# redis_client = redis.Redis(host='localhost', port=6379, db=0)

# Подключение к PostgreSQL и автоматическое заполнение
async def init_db():
    global pool  # Используем глобальную переменную
    try:
        # Создаем пул подключений
        pool = await asyncpg.create_pool(DATABASE_URL)

        # Создаем таблицу воспоминаний, если она не существует
        async with pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS memories (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    date TEXT NOT NULL,
                    place TEXT,
                    rating INTEGER,
                    description TEXT,
                    photo_path TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')

        print("Таблица 'memories' успешно создана/проверена")
        return pool
    except Exception as e:
        print(f"Ошибка при создании таблицы: {e}")
        raise


# Начальное меню
async def show_main_menu(message: Message, text: str = None):
    builder = ReplyKeyboardBuilder()
    builder.row(
        types.KeyboardButton(text="Поехали!"),
        types.KeyboardButton(text="На память")
    )
    builder.row(types.KeyboardButton(text="История"))

    if text is None:
        text = "Привет! Я помогу тебе спланировать день и сохранить воспоминания."

    await message.answer(
        text,
        reply_markup=builder.as_markup(resize_keyboard=True)
    )


# Обработчик команды /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await show_main_menu(message)


# Обработчик кнопки "Поехали!"
@dp.message(F.text == "Поехали!")
async def ask_interests(message: Message):
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="Концерты", callback_data="category_concert"),
        types.InlineKeyboardButton(text="Выставки", callback_data="category_exhibition"),
        types.InlineKeyboardButton(text="Развлечения", callback_data="category_fun")
    )
    builder.row(types.InlineKeyboardButton(text="Назад", callback_data="main_menu"))

    await message.answer(
        "Что вас интересует?",
        reply_markup=builder.as_markup()
    )

# Простая реализация "кэша" в памяти для текущего пользователя
async def get_events_from_cache(user_id: int):
    events = events_cache.get(user_id)
    print(f"[DEBUG] Events in cache for user {user_id}: {len(events) if events else 0}")
    return events


# Получение событий из KudaGo API
async def get_events(category: str, date_input: str):
    """Улучшенная версия функции для получения событий"""
    params = {
        'location': 'spb',
        'page_size': 20,  # Было 0 - это ошибка, получали 0 событий
        'lang': 'ru',
        'fields': 'id,title,place,price,images,site_url,description',  # Явно запрашиваем нужные поля
        'expand': 'place',  # Получаем полную информацию о месте
        'text_format': 'plain'  # Убираем HTML-разметку в описаниях
    }

    # Определяем категорию
    category_map = {
        'concert': 'concert',
        'exhibition': 'exhibition',
        'fun': 'entertainment'
    }
    params['categories'] = category_map.get(category, 'all')

    # Обрабатываем разные форматы даты
    try:
        if date_input == 'today':
            since = datetime.now()
            until = since + timedelta(days=1)
        elif date_input == 'tomorrow':
            since = datetime.now() + timedelta(days=1)
            until = since + timedelta(days=1)
        else:
            # Для ручного ввода даты
            since = datetime.strptime(date_input, "%d.%m.%Y")
            until = since + timedelta(days=1)

        # Форматируем временные метки
        params['actual_since'] = int(since.timestamp())
        params['actual_until'] = int(until.timestamp())

        # Делаем запрос с таймаутом
        response = requests.get(
            'https://kudago.com/public-api/v1.4/events/',
            params=params,
            timeout=10
        )
        
        # Проверяем статус ответа
        if response.status_code != 200:
            print(f"API вернуло статус {response.status_code}")
            return []
            
        data = response.json()
        
        # Проверяем наличие результатов
        if not data.get('results'):
            print("API вернуло пустой список событий")
            return []
            
        return data['results']

    except Exception as e:
        print(f"Ошибка при запросе к API: {e}")
        return []


# Отображение карточки события
async def show_event_card(chat_id: int, events: list, index: int):
    try:
        if index < 0 or index >= len(events):
            raise IndexError("Invalid event index")

        event = events[index]
        print(f"[DEBUG] Event structure: {event}")  # Для отладки

        # Формирование текста
        title = event.get('title', 'Без названия')
        
        # Обработка места
        place_data = event.get('place', {})
        if isinstance(place_data, dict):
            place_name = place_data.get('name', '')
            address = place_data.get('address', '')
            place_text = f"{place_name}, {address}" if place_name else address
        else:
            place_text = str(place_data) if place_data else 'Место не указано'
        
        # Обработка цены
        price_data = event.get('price', '')
        if isinstance(price_data, dict):
            price_text = price_data.get('name', 'Цена не указана')
        else:
            price_text = str(price_data) if price_data else 'Цена не указана'

        url = event.get('site_url', 'https://kudago.com')
        
        text = (
            f"🎟 <b>{title}</b>\n\n"
            f"📍 <b>Место:</b> {place_text if place_text else 'Адрес не указан'}\n"
            f"💰 <b>Цена:</b> {price_text}\n"
            f"🌐 <b>Сайт:</b> <a href='{url}'>Подробнее</a>"
        )

        # Обработка изображения
        image_url = None
        images = event.get('images', [])
        if images and isinstance(images, list):
            first_image = images[0]
            if isinstance(first_image, dict):
                image_url = first_image.get('image')

        # Клавиатура
        builder = InlineKeyboardBuilder()
        if index > 0:
            builder.button(text="◀ Назад", callback_data=f"event_prev_{index}")
        if index < len(events) - 1:
            builder.button(text="Дальше ▶", callback_data=f"event_next_{index}")
        builder.button(text="🏠 Меню", callback_data="main_menu")
        builder.adjust(2)

        # Отправка сообщения
        if image_url:
            try:
                # Скачиваем изображение и отправляем как файл
                response = requests.get(image_url)
                if response.status_code == 200:
                    photo = types.BufferedInputFile(response.content, filename="event.jpg")
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=text,
                        reply_markup=builder.as_markup(),
                        parse_mode='HTML'
                    )
                    return
            except Exception as e:
                print(f"[ERROR] Failed to send photo: {e}")

        # Если изображение не удалось отправить, отправляем текст
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode='HTML'
        )

    except Exception as e:
        print(f"[ERROR] Failed to show event card: {e}")
        await bot.send_message(
            chat_id=chat_id,
            text="⚠ Произошла ошибка при загрузке информации о мероприятии",
            reply_markup=types.ReplyKeyboardRemove()
        )

# Обработчик выбора категории
@dp.callback_query(F.data.startswith("category_"))
async def choose_date(callback: CallbackQuery):
    category = callback.data.split("_")[1]

    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="Сегодня", callback_data=f"date_today_{category}"),
        types.InlineKeyboardButton(text="Завтра", callback_data=f"date_tomorrow_{category}")
    )
    builder.row(
        types.InlineKeyboardButton(text="Ввести дату", callback_data=f"date_custom_{category}"),
        types.InlineKeyboardButton(text="Назад", callback_data="back_to_interests")
    )

    await callback.message.edit_text(
        "Выберите дату",
        reply_markup=builder.as_markup()
    )


# Обработчик выбора даты
@dp.callback_query(F.data.startswith("date_"))
async def handle_date_selection(callback: CallbackQuery,  state: FSMContext):
    print("[DEBUG] Вызван handle_date_selection")
    data = callback.data.split("_")
    date_type = data[1]
    category = data[2]

    if date_type == 'today':
        events = await get_events(category, 'today')
    elif date_type == 'tomorrow':
        events = await get_events(category, 'tomorrow')
    elif date_type == 'custom':
        await state.update_data(category=category)
        await callback.message.answer("Введите дату в формате ДД.ММ.ГГГГ")
        await state.set_state(MemoryStates.waiting_for_custom_date)
        return

    if not events:
        await callback.message.answer("На выбранную дату мероприятий не найдено 😢")
        return

    user_id = callback.from_user.id
    events_cache[user_id] = events
    print(f"[DEBUG] Saved {len(events)} events to cache for user {user_id}")
    current_event_index[user_id] = 0
    await show_event_card(user_id, events, 0)

    await callback.message.delete()


# Обработчик выбора даты
@dp.callback_query(F.data.startswith("date_custom_"))
async def handle_custom_date_input(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[2]
    await state.update_data(category=category)
    await callback.message.answer("Введите дату в формате ДД.ММ.ГГГГ")
    await state.set_state(MemoryStates.waiting_for_custom_date)  # Используем новое состояние
    await callback.message.delete()


# Обработчик ввода даты вручную
@dp.message(MemoryStates.waiting_for_custom_date)
async def process_custom_date(message: Message, state: FSMContext):
    try:
        date_str = message.text
        # Проверяем формат даты
        date_obj = datetime.strptime(date_str, "%d.%m.%Y")
        data = await state.get_data()
        category = data.get('category')

        # Получаем события для введенной даты
        events = await get_events(category, date_str)  # Передаем строку с датой

        if not events:
            await message.answer("На выбранную дату мероприятий не найдено 😢")
            await state.clear()
            return

        user_id = message.from_user.id
        events_cache[user_id] = events
        
        current_event_index[user_id] = 0
        await show_event_card(user_id, events, 0)
        await state.clear()


    except ValueError:
        await message.answer("Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ")


# Обработчик навигации по событиям
@dp.callback_query(F.data.startswith("event_"))
async def handle_event_navigation(callback: CallbackQuery):
    data = callback.data.split("_")
    direction = data[1]
    current_index = int(data[2])
    user_id = callback.from_user.id

    # Получаем события из "кэша" (в реальном проекте нужно реализовать кэширование)
    events = await get_events_from_cache(user_id)
    if not events:
        await callback.answer("Мероприятия не найдены")
        return

    if direction == 'prev':
        new_index = current_index - 1
    else:
        new_index = current_index + 1

    current_event_index[user_id] = new_index
    await callback.message.delete()
    await show_event_card(user_id, events, new_index)


# Обработчик кнопки "На память"
@dp.message(F.text == "На память")
async def start_memory_creation(message: Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(
            text="Сегодня", 
            callback_data="memory_date_today"
        ),
        types.InlineKeyboardButton(
            text="Другая дата", 
            callback_data="memory_date_custom"
        )
    )
    builder.row(types.InlineKeyboardButton(
        text="Назад", 
        callback_data="main_menu"
    ))

    await message.answer(
        "📅 Выберите дату воспоминания:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(MemoryStates.waiting_for_date)

@dp.callback_query(F.data == "memory_date_today")
async def handle_memory_date_today(callback: CallbackQuery, state: FSMContext):
    today = datetime.now().strftime("%d.%m.%Y")
    await state.update_data(date=today)
    await callback.message.answer("🏛 Напишите название места/локации:")
    await state.set_state(MemoryStates.waiting_for_place)
    await callback.message.delete()

@dp.callback_query(F.data == "memory_date_custom")
async def handle_memory_date_custom(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📆 Введите дату в формате ДД.ММ.ГГГГ:")
    await state.set_state(MemoryStates.waiting_for_date)
    await callback.message.delete()

# Обработчик даты для воспоминания

@dp.message(MemoryStates.waiting_for_date)
async def process_memory_date(message: Message, state: FSMContext):
    try:
        user_date = datetime.strptime(message.text, "%d.%m.%Y").date()
        today = date_class.today()

        if user_date > today:
            await message.answer("❌ Вы не можете сохранить воспоминание из будущего.\nПожалуйста, введите дату не позже сегодняшней.")
            return

        await state.update_data(date=user_date.strftime("%d.%m.%Y"))
        await message.answer("🏛 Напишите название места/локации:")
        await state.set_state(MemoryStates.waiting_for_place)
    except ValueError:
        await message.answer("❌ Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ.")

# Обработчик места для воспоминания
@dp.message(MemoryStates.waiting_for_place)
async def process_memory_place(message: Message, state: FSMContext):
    await state.update_data(place=message.text)

    builder = InlineKeyboardBuilder()
    for i in range(1, 11):
        builder.button(text=str(i), callback_data=f"rating_{i}")
    builder.button(text="Назад", callback_data="back_to_date")

    await message.answer(
        "Оцените ваш день",
        reply_markup=builder.as_markup()
    )
    await state.set_state(MemoryStates.waiting_for_rating)


# Обработчик оценки для воспоминания
@dp.callback_query(F.data.startswith("rating_"), MemoryStates.waiting_for_rating)
async def process_memory_rating(callback: CallbackQuery, state: FSMContext):
    try:
        rating = int(callback.data.split("_")[1])
        if 1 <= rating <= 10:
            await state.update_data(rating=rating)
            await callback.message.answer("📝 Опишите свои эмоции и впечатления:")
            await state.set_state(MemoryStates.waiting_for_description)
            await callback.message.delete()
        else:
            await callback.answer("Пожалуйста, выберите оценку от 1 до 10")
    except (IndexError, ValueError):
        await callback.answer("Неверный формат оценки")


@dp.callback_query(F.data == "skip_description", MemoryStates.waiting_for_description)
async def skip_description(callback: CallbackQuery, state: FSMContext):
    await state.update_data(description=None)

    builder = InlineKeyboardBuilder()
    builder.button(text="Пропустить", callback_data="skip_photo")

    await callback.message.answer(
        "📸 Пришлите фото дня",
        reply_markup=builder.as_markup()
    )
    await state.set_state(MemoryStates.waiting_for_photo)
    await callback.message.delete()



# Обработчик описания для воспоминания
@dp.message(MemoryStates.waiting_for_description)
async def process_memory_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)

    builder = InlineKeyboardBuilder()
    builder.button(text="Пропустить", callback_data="skip_photo")

    await message.answer(
        "📸 Пришлите фото дня",
        reply_markup=builder.as_markup()
    )
    await state.set_state(MemoryStates.waiting_for_photo)


@dp.message(MemoryStates.waiting_for_photo)
async def process_memory_photo(message: Message, state: FSMContext):
    global pool

    data = await state.get_data()
    
    # Если фото не прикреплено (например, текст вместо фото)
    if not message.photo:
        await message.answer("📸 Пожалуйста, прикрепите фото или нажмите 'Пропустить'")
        return

    photo = message.photo[-1]
    photo_file = await bot.get_file(photo.file_id)
    photo_path = f"photos/{message.from_user.id}_{int(time.time())}.jpg"

    os.makedirs("photos", exist_ok=True)
    await bot.download_file(photo_file.file_path, photo_path)

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO memories 
            (user_id, date, place, rating, description, photo_path) 
            VALUES ($1, $2, $3, $4, $5, $6)""",
            message.from_user.id,
            data.get('date'),
            data.get('place'),
            data.get('rating'),
            data.get('description'),
            photo_path
        )

    await message.answer("✅ Воспоминание успешно сохранено с фото!")
    await state.clear()
    await show_main_menu(message)

@dp.callback_query(F.data == "skip_photo", MemoryStates.waiting_for_photo)
async def skip_photo(callback: CallbackQuery, state: FSMContext):
    global pool

    data = await state.get_data()

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO memories 
            (user_id, date, place, rating, description) 
            VALUES ($1, $2, $3, $4, $5)""",
            callback.from_user.id,
            data.get('date'),
            data.get('place'),
            data.get('rating'),
            data.get('description')
        )

    await callback.message.answer("✅ Воспоминание сохранено без фото!")
    await state.clear()
    await show_main_menu(callback.message)



# Обработчик кнопки "История"
@dp.message(F.text == "История")
async def show_history_periods(message: Message):
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="Неделя", callback_data="history_week"),
        types.InlineKeyboardButton(text="Месяц", callback_data="history_month")
    )
    builder.row(
        types.InlineKeyboardButton(text="Выбрать период", callback_data="history_custom"),
        types.InlineKeyboardButton(text="Назад", callback_data="main_menu")
    )

    await message.answer(
        "За какой период показать историю?",
        reply_markup=builder.as_markup()
    )


# Получение воспоминаний из БД (теперь без параметра pool)
async def get_memories(user_id: int, period: str = None, start_date: str = None, end_date: str = None):
    global pool

    query = "SELECT * FROM memories WHERE user_id = $1"
    params = [user_id]

    if period == 'week':
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=7)
        query += " AND TO_DATE(date, 'DD.MM.YYYY') BETWEEN $2 AND $3"
        params.extend([start_date, end_date])
    elif period == 'month':
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=30)
        query += " AND TO_DATE(date, 'DD.MM.YYYY') BETWEEN $2 AND $3"
        params.extend([start_date, end_date])
    elif start_date and end_date:
        query += " AND TO_DATE(date, 'DD.MM.YYYY') BETWEEN $2 AND $3"
        params.extend([start_date, end_date])

    query += " ORDER BY TO_DATE(date, 'DD.MM.YYYY') DESC"

    async with pool.acquire() as conn:
        return await conn.fetch(query, *params)


# Отображение карточки воспоминания
async def show_memory_card(chat_id: int, memories: list, index: int):
    memory = memories[index]

    text = (
        f"📅 <b>Дата:</b> {memory['date']}\n"
        f"📍 <b>Место:</b> {memory['place'] or 'Не указано'}\n"
        f"⭐ <b>Оценка:</b> {memory['rating'] or 'Не указана'}\n"
        f"📝 <b>Описание:</b> {memory['description'] or 'Не указано'}"
    )

    # Клавиатура
    builder = InlineKeyboardBuilder()
    if index > 0:
        builder.button(text="Назад", callback_data=f"memory_prev_{index}")
    if index < len(memories) - 1:
        builder.button(text="Дальше", callback_data=f"memory_next_{index}")
    builder.button(text="Меню", callback_data="main_menu")

    # Отправка сообщения
    if memory['photo_path'] and os.path.exists(memory['photo_path']):
        await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile(memory['photo_path']),
            caption=text,
            reply_markup=builder.as_markup(),
            parse_mode='HTML'
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode='HTML'
        )


# Обработчик выбора периода истории (теперь без параметра pool)
@dp.callback_query(F.data.startswith("history_"))
async def handle_history_period(callback: CallbackQuery, state: FSMContext):
    period = callback.data.split("_")[1]

    if period == 'custom':
        await callback.message.answer("Введите начальную дату периода (ДД.ММ.ГГГГ)")
        await state.set_state(MemoryStates.waiting_for_start_date)
        return

    if period == 'week':
        memories = await get_memories(callback.from_user.id, 'week')
        
    elif period == 'month':
        memories = await get_memories(callback.from_user.id, 'month')

    if not memories:
        await callback.message.answer("За выбранный период воспоминаний не найдено")
        return
    
    memories_cache[callback.from_user.id] = memories

    current_memory_index[callback.from_user.id] = 0
    await show_memory_card(callback.from_user.id, memories, 0)
    await callback.message.delete()


# Обработчик ввода начальной даты периода
@dp.message(MemoryStates.waiting_for_start_date)
async def process_start_date(message: Message, state: FSMContext):
    try:
        start_date = datetime.strptime(message.text, "%d.%m.%Y").date()
        await state.update_data(start_date=start_date)
        await message.answer("Введите конечную дату периода (ДД.ММ.ГГГГ)")
        await state.set_state(MemoryStates.waiting_for_end_date)
    except ValueError:
        await message.answer("Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ")


# Обработчик ввода конечной даты периода (теперь без параметра pool)
@dp.message(MemoryStates.waiting_for_end_date)
async def process_end_date(message: Message, state: FSMContext):
    try:
        end_date = datetime.strptime(message.text, "%d.%m.%Y").date()
        data = await state.get_data()
        start_date = data.get('start_date')

        if start_date > end_date:
            await message.answer("Начальная дата должна быть раньше конечной")
            return

        memories = await get_memories(
            message.from_user.id,
            start_date=start_date,
            end_date=end_date
        )

        if not memories:
            await message.answer("За выбранный период воспоминаний не найдено")
            return

        memories_cache[message.from_user.id] = memories
        current_memory_index[message.from_user.id] = 0
        await show_memory_card(message.from_user.id, memories, 0)
        await state.clear()

    except ValueError:
        await message.answer("Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ")


# Обработчик навигации по воспоминаниям (теперь без параметра pool)
@dp.callback_query(F.data.startswith("memory_"))
async def handle_memory_navigation(callback: CallbackQuery):
    data = callback.data.split("_")
    direction = data[1]
    current_index = int(data[2])
    user_id = callback.from_user.id

    # Получаем воспоминания из "кэша"
    memories = await get_memories_from_cache(user_id)
    if not memories:
        await callback.answer("Воспоминания не найдены")
        return

    if direction == 'prev':
        new_index = current_index - 1
    else:
        new_index = current_index + 1

    current_memory_index[user_id] = new_index
    await callback.message.delete()
    await show_memory_card(user_id, memories, new_index)


# Обработчик возврата в меню
@dp.callback_query(F.data == "main_menu")
async def back_to_main_menu(callback: CallbackQuery):
    await callback.message.delete()
    await show_main_menu(callback.message)


async def get_events_from_cache(user_id: int):
    """Получение событий из кэша"""
    return events_cache.get(user_id)

async def get_memories_from_cache(user_id: int):
    """Получение воспоминаний из кэша"""
    return memories_cache.get(user_id)


# Запуск бота с подключением к БД
async def main():
    global pool
    pool = await init_db()  # Инициализация БД перед запуском
    await dp.start_polling(bot)

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())