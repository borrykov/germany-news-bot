"""Public Telegram web collector. No Telegram account or bot token is used here."""
import time
import urllib.request
import urllib.error
from html.parser import HTMLParser
from datetime import datetime


class PageParser(HTMLParser):
    """Track nested elements so captions retain paragraphs and dates stay attached."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.current = None
        self.posts = []
        self.text_depth = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if 'tgme_widget_message' in classes and attrs.get('data-post'):
            self.current = {'key': attrs['data-post'], 'text': '', 'date': None}
        if tag == 'time' and self.current and attrs.get('datetime'):
            self.current['date'] = attrs['datetime']
        if 'tgme_widget_message_text' in classes and self.current:
            self.text_depth = len(self.stack)
        if tag == 'br' and self.text_depth is not None and self.current:
            self.current['text'] += '\n'
        # Void HTML elements have no matching end tag.
        if tag not in {'br', 'img', 'meta', 'link', 'input', 'hr', 'source', 'wbr'}:
            self.stack.append((tag, 'tgme_widget_message' in classes))

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack)-1, -1, -1)
                      if self.stack[i][0] == tag), None)
        if index is None:
            return
        closed = self.stack[index:]
        if self.text_depth is not None and index <= self.text_depth:
            self.text_depth = None
        if any(is_message for _, is_message in closed) and self.current:
            self.current['text'] = self.current['text'].strip()
            self.posts.append(self.current)
            self.current = None
        del self.stack[index:]

    def handle_data(self, data):
        if self.current and self.text_depth is not None:
            self.current['text'] += data


def fetch(url):
    """Bound retries; never silently interpret a transport error as an empty feed."""
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'GermanyNewsBot/1.0'})
            with urllib.request.urlopen(req, timeout=35) as response:
                if '/s/' not in response.url:
                    raise RuntimeError('Публичный просмотр недоступен: перенаправление')
                return response.read().decode('utf-8')
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError('Источник вернул HTTP %s' % error.code) from None
            delay = error.headers.get('Retry-After', '')
            time.sleep(min(float(delay) if delay.isdigit() else 2**attempt, 60))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise RuntimeError('Источник недоступен после повторных попыток') from None
            time.sleep(2**attempt)


def collect(channel, start, end, config, save):
    """Completeness means the visible timeline crossed start, not a full archive guarantee."""
    before, seen, selected = None, set(), {}
    media_only = 0
    for _ in range(config['max_pages_per_channel']):
        url = 'https://t.me/s/' + channel
        if before:
            url += '?before=' + str(before)
        parser = PageParser()
        parser.feed(fetch(url))
        if not parser.posts:
            return list(selected.values()), False, 'Нет распознаваемых публикаций', media_only
        dated = []
        fresh_ids = []
        for post in parser.posts:
            try:
                handle, raw_id = post['key'].split('/')
                number = int(raw_id)
                date = datetime.fromisoformat(post['date'].replace('Z', '+00:00'))
                if date.tzinfo is None or handle.lower() != channel.lower():
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
            dated.append(date)
            if number in seen:
                continue
            seen.add(number)
            fresh_ids.append(number)
            post['url'] = 'https://t.me/' + post['key']
            save(post)
            if start <= date < end:
                selected[post['key']] = post
                if not post['text']:
                    media_only += 1
        if not dated or not fresh_ids:
            return list(selected.values()), False, 'История не продвигается или даты не прочитаны', media_only
        # Require the entire page to be older, avoiding early exit on a pinned old post.
        if max(dated) < start:
            return list(selected.values()), True, 'Граница периода достигнута', media_only
        next_before = min(fresh_ids)
        if before is not None and next_before >= before:
            return list(selected.values()), False, 'Повтор страницы', media_only
        before = next_before
        time.sleep(config['request_delay'])
    return list(selected.values()), False, 'Достигнут предел страниц', media_only
