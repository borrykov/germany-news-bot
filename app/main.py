"""Single-owner Telegram bot. Standard library only; run with python3 -m app.main."""
import argparse
import fcntl
import getpass
import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .source import collect
from .analysis import analyze

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def now():
    return datetime.now(UTC)


def journal(message):
    """Append operational milestones without credentials or publication text."""
    with (ROOT/'DEVELOPMENT_HISTORY.txt').open('a') as file:
        file.write(now().isoformat()+' '+message+'\n')


def connect():
    db = sqlite3.connect(str(ROOT/'data/state.sqlite3'))
    db.executescript('''
    CREATE TABLE IF NOT EXISTS posts (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS requests
      (id INTEGER PRIMARY KEY, start TEXT, end TEXT, status TEXT, report TEXT);
    ''')
    return db


def split_text(text, limit=3500):
    # Plain text deliberately avoids broken Markdown/HTML and escaping surprises.
    while len(text) > limit:
        cut = text.rfind('\n', 0, limit)
        if cut < limit//2:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip('\n')
    if text:
        yield text


def custom_period(text, tz, end):
    """Inclusive local dates become a half-open UTC interval, including DST changes."""
    parts = text.split()
    if len(parts) != 2:
        raise ValueError('Нужны две даты: 2026-11-10 2026-11-16')
    first, last = [datetime.strptime(part, '%Y-%m-%d').date() for part in parts]
    start = datetime.combine(first, datetime.min.time(), tz).astimezone(UTC)
    stop = datetime.combine(last+timedelta(days=1), datetime.min.time(), tz).astimezone(UTC)
    if first > last or start >= end:
        raise ValueError('Начало должно быть не позже конца и раньше текущего времени')
    return start, min(stop, end)


class Bot:
    def __init__(self, config):
        self.config = config
        self.tz = ZoneInfo(config['timezone'])
        self.token = (ROOT/'.env/bot_token').read_text().strip()
        owner_value = (ROOT/'.env/owner_id').read_text().strip()
        if not self.token or not owner_value:
            raise RuntimeError('Сначала запусти python3 -m app.main --setup')
        if owner_value.isdigit():
            self.owner = int(owner_value)
        else:
            # Bootstrap waits for /start rather than assuming the inbox is populated.
            # A mismatching username can only be accepted locally at the Mac keyboard.
            username = owner_value.lstrip('@').lower()
            me = self.api('getMe', {})
            print('Бот: @'+me.get('username', '?'), flush=True)
            print('Ожидаю /start от @'+username+' (до 3 минут).', flush=True)
            deadline = time.monotonic()+180
            offset = 0
            owner = None
            reviewed = set()
            while time.monotonic() < deadline and owner is None:
                updates = self.api('getUpdates', {'offset': offset, 'timeout': 20,
                                   'allowed_updates': ['message', 'callback_query']})
                for update in updates:
                    offset = max(offset, update['update_id']+1)
                    message = update.get('message', {})
                    sender = message.get('from', {})
                    command = message.get('text', '').strip().split()
                    if (message.get('chat', {}).get('type') != 'private'
                        or not command or command[0].split('@')[0] != '/start'
                        or not isinstance(sender.get('id'), int)):
                        continue
                    actual = sender.get('username', '')
                    if actual.lower() == username:
                        owner = sender['id']
                        break
                    if sender['id'] in reviewed:
                        continue
                    reviewed.add(sender['id'])
                    print('Получен /start: @%s, имя: %s, ID: %s' %
                          (actual or '(нет username)', sender.get('first_name', ''), sender['id']), flush=True)
                    answer = input('Это твой аккаунт? Для подтверждения введи указанный ID, иначе Enter: ').strip()
                    if answer == str(sender['id']):
                        owner = sender['id']
                        break
            if owner is None:
                raise RuntimeError('Не получен подтверждённый /start. Проверь имя бота выше и запусти ещё раз')
            self.owner = owner
            path = ROOT/'.env/owner_id'
            path.write_text(str(self.owner)+'\n')
            path.chmod(0o600)
            journal('Числовой ID владельца получен из личного /start; username заменён ID')
        if self.owner <= 0:
            raise RuntimeError('Нужен положительный ID владельца')
        self.lock = threading.Lock()
        self.wait_dates = False
        self.worker = None

    def api(self, method, payload, timeout=45):
        # Never log the URL: Telegram embeds the bot credential in it.
        req = urllib.request.Request('https://api.telegram.org/bot'+self.token+'/'+method,
                                     data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError('Telegram HTTP %s (%s)' % (error.code, method)) from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError('Сеть Telegram недоступна (%s)' % method) from None
        if not data.get('ok'):
            raise RuntimeError('Telegram отклонил запрос (%s)' % method)
        return data['result']

    def send(self, text, keyboard=None):
        for part in split_text(text):
            payload = {'chat_id': self.owner, 'text': part, 'disable_web_page_preview': True}
            if keyboard:
                payload['reply_markup'] = keyboard
            self.api('sendMessage', payload)
            time.sleep(0.1)

    def menu(self):
        rows = [[('24 часа', 'days:1'), ('3 дня', 'days:3'), ('7 дней', 'days:7')],
                [('С прошлого анализа', 'since'), ('Свои даты', 'custom')]]
        keyboard = {'inline_keyboard': [[{'text': title, 'callback_data': value}
                                        for title, value in row] for row in rows]}
        self.send('Новости: электроэнергетика, внутренняя и внешняя политика Германии.\n'
                  'Выбери период. Даты: '+self.config['timezone'], keyboard)

    def start_job(self, start, end):
        if end-start > timedelta(days=self.config['max_range_days']):
            self.send('Максимальный период: %s дней' % self.config['max_range_days'])
            return
        if not self.lock.acquire(blocking=False):
            self.send('Предыдущий анализ ещё выполняется.')
            return
        self.worker = threading.Thread(target=self.job, args=(start, end))
        self.worker.start()

    def job(self, start, end):
        db, request_id = None, None
        try:
            db = connect()
            cursor = db.execute('INSERT INTO requests(start,end,status) VALUES(?,?,?)',
                                (start.isoformat(), end.isoformat(), 'running'))
            request_id = cursor.lastrowid
            db.commit()
            journal('Начат запрос %s: %s — %s' % (request_id, start, end))
            self.send('Собираю новости. Период: %s — %s' %
                      (start.astimezone(self.tz).strftime('%d.%m.%Y %H:%M'),
                       end.astimezone(self.tz).strftime('%d.%m.%Y %H:%M')))
            posts, coverage, complete, media = [], [], True, 0

            def save(post):
                db.execute('INSERT OR REPLACE INTO posts VALUES(?,?)',
                           (post['key'], json.dumps(post, ensure_ascii=False)))

            for channel in self.config['channels']:
                self.send('Читаю @'+channel)
                try:
                    found, ok, note, skipped = collect(channel, start, end, self.config, save)
                    posts.extend(found)
                    media += skipped
                    complete = complete and ok
                    coverage.append('@%s: %s постов; %s' % (channel, len(found), note))
                except Exception as error:
                    complete = False
                    coverage.append('@%s: ошибка сбора (%s)' % (channel, type(error).__name__))
                db.commit()
            text_posts = sorted((p for p in posts if p['text']), key=lambda p: p['date'])
            events = analyze(ROOT, text_posts, self.config, self.send)
            header = ('Германия: %s — %s\nПроверено текстовых постов: %s. Событий: %s.\n%s\n'
                      'Постов без доступного текста: %s. Фото, видео и внешние статьи не анализировались.\n'
                      'Полнота относится только к доступной публичной ленте.\n' %
                      (start.astimezone(self.tz).strftime('%d.%m.%Y %H:%M'),
                       end.astimezone(self.tz).strftime('%d.%m.%Y %H:%M'),
                       len(text_posts), len(events), '\n'.join(coverage), media))
            messages = [header]
            for index, event in enumerate(events, 1):
                links = '\n'.join('https://t.me/'+key for key in dict.fromkeys(event['source_keys']))
                messages.append('%s. %s\n\n%s\n\nИсточники:\n%s' %
                                (index, event['title'], '\n'.join('• '+x for x in event['details']), links))
            if not events:
                messages.append('Подходящих новостей в доступных текстах не найдено.')
            if not complete:
                messages.append('Результат частичный. Отметка прошлого успешного анализа не изменена.')
            report = ROOT/'data'/('report-%s.json' % request_id)
            report.write_text(json.dumps({'start': start.isoformat(), 'end': end.isoformat(),
                                         'coverage': coverage, 'complete': complete, 'events': events},
                                        ensure_ascii=False, indent=2))
            db.execute('UPDATE requests SET status=?,report=? WHERE id=?',
                       ('delivering', str(report), request_id))
            db.commit()
            # Checkpoint advances only after every message has been accepted by Telegram.
            # A network failure during sending may leave delivered parts; report is preserved.
            for message in messages:
                self.send(message)
            if complete:
                previous = db.execute("SELECT value FROM state WHERE key='last_success'").fetchone()
                # A historical custom range must never move the checkpoint backwards.
                if not previous or datetime.fromisoformat(previous[0]) < end:
                    db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', ('last_success', end.isoformat()))
            db.execute('UPDATE requests SET status=? WHERE id=?',
                       ('complete' if complete else 'partial', request_id))
            db.commit()
            journal('Завершён запрос %s; complete=%s; events=%s' % (request_id, complete, len(events)))
        except Exception as error:
            journal('Ошибка запроса %s: %s' % (request_id, type(error).__name__))
            if db and request_id:
                db.execute('UPDATE requests SET status=? WHERE id=?', ('failed', request_id))
                db.commit()
            try:
                # Known application errors are sanitized; unexpected exception text stays private.
                message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                self.send('Анализ не завершён: '+message+'\nОтметка прошлого анализа не изменена.')
            except Exception:
                pass
        finally:
            if db:
                db.close()
            self.lock.release()

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if callback else update.get('message', {})
        sender = callback.get('from', {}) if callback else message.get('from', {})
        # Authorization uses immutable IDs, never a username. Groups are excluded.
        if sender.get('id') != self.owner or message.get('chat', {}).get('id') != self.owner:
            return
        action = callback.get('data', '') if callback else message.get('text', '')
        if callback:
            self.api('answerCallbackQuery', {'callback_query_id': callback['id']})
        if action in ('/start', '/menu', '/help'):
            self.wait_dates = False
            self.menu()
        elif action == 'custom':
            self.wait_dates = True
            self.send('Введи две даты включительно: 2026-11-10 2026-11-16\nЧасовой пояс: '+self.config['timezone'])
        elif action.startswith('days:') and action[5:] in ('1', '3', '7'):
            self.wait_dates = False
            end = now()
            self.start_job(end-timedelta(days=int(action[5:])), end)
        elif action == 'since':
            self.wait_dates = False
            with connect() as db:
                row = db.execute("SELECT value FROM state WHERE key='last_success'").fetchone()
            if not row:
                self.send('Успешных анализов ещё нет. Сначала выбери период, например 24 часа.')
            else:
                self.start_job(datetime.fromisoformat(row[0]), now())
        elif self.wait_dates and not callback:
            try:
                start, end = custom_period(action, self.tz, now())
            except ValueError as error:
                self.send('Некорректные даты. '+str(error))
                return
            self.wait_dates = False
            self.start_job(start, end)
        else:
            self.menu()

    def run(self):
        self.api('getMe', {})
        webhook = self.api('getWebhookInfo', {})
        if webhook.get('url'):
            raise RuntimeError('У бота уже есть webhook. Используй нового бота или отключи webhook отдельно')
        with connect() as db:
            db.execute("UPDATE requests SET status='interrupted' WHERE status IN ('running','delivering')")
            row = db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        offset = int(row[0]) if row else 0
        journal('Бот запущен вручную')
        print('Бот запущен. В Telegram отправь /start. Остановка: Ctrl+C.', flush=True)
        self.menu()
        while True:
            try:
                updates = self.api('getUpdates', {'offset': offset, 'timeout': 25,
                                   'allowed_updates': ['message', 'callback_query']})
                for update in updates:
                    # Old clicks should not start a surprise analysis after a long shutdown.
                    message = update.get('message') or update.get('callback_query', {}).get('message', {})
                    if now().timestamp()-message.get('date', 0) < 600 or 'callback_query' in update:
                        self.handle(update)
                    offset = update['update_id']+1
                    with connect() as db:
                        db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', ('offset', str(offset)))
            except RuntimeError as error:
                print(str(error), flush=True)
                time.sleep(5)


def setup():
    """Enter secrets locally; never ask the user to paste credentials in the chat."""
    token = getpass.getpass('Токен от BotFather (ввод скрыт): ').strip()
    owner = input('Твой числовой Telegram ID: ').strip()
    if ':' not in token or not owner.isdigit() or int(owner) <= 0:
        raise RuntimeError('Некорректный токен или ID')
    for name, value in [('bot_token', token), ('owner_id', owner)]:
        path = ROOT/'.env'/name
        path.write_text(value+'\n')
        path.chmod(0o600)
    journal('Локальная настройка токена и владельца завершена')
    print('Настройки сохранены в скрытой папке .env.')


def main():
    os.umask(0o077)
    for name in ('.env', 'data'):
        (ROOT/name).mkdir(exist_ok=True, mode=0o700)
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup', action='store_true')
    args = parser.parse_args()
    if args.setup:
        setup()
        return
    # Prevent duplicate pollers and concurrent writes from two launched copies.
    with (ROOT/'data/process.lock').open('w') as process_lock:
        try:
            fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Бот уже запущен') from None
        status = subprocess.run(['codex', 'login', 'status'], capture_output=True, text=True)
        if status.returncode or 'ChatGPT' not in status.stdout+status.stderr:
            raise RuntimeError('Выполни codex login и войди через ChatGPT')
        bot = Bot(json.loads((ROOT/'config.json').read_text()))
        try:
            bot.run()
        except KeyboardInterrupt:
            print('\nОстановка. Если анализ выполняется, ожидаю его завершения.', flush=True)
            if bot.worker:
                bot.worker.join()
            journal('Бот остановлен')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        print('Ошибка запуска: '+str(error))
        raise SystemExit(1)
