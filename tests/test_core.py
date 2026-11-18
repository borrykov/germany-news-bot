import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo
from app.source import PageParser, collect
from app.main import custom_period, split_text
from app.main import Bot
from app.analysis import validate


def page(number, date, text='Текст'):
    return ('<div class="tgme_widget_message" data-post="test/%s">'
            '<div class="tgme_widget_message_text"><b>%s</b><br>Строка &amp; ещё</div>'
            '<a><time datetime="%s"></time></a></div>') % (number, text, date)


class CoreTests(unittest.TestCase):
    def test_bootstrap_waits_for_start(self):
        updates = [{'update_id': 12, 'message': {'chat': {'type': 'private'},
                   'from': {'id': 123, 'username': 'EXAMPLE_USER'}, 'text': '/start'}}]
        with patch('app.main.Path.read_text', side_effect=['token', 'EXAMPLE_USER']), \
             patch.object(Bot, 'api', side_effect=[{'username': 'testbot'}, [], updates]), \
             patch('app.main.Path.write_text') as write, \
             patch('app.main.Path.chmod'), patch('app.main.journal'):
            bot = Bot({'timezone': 'Europe/Moscow'})
        self.assertEqual(bot.owner, 123)
        write.assert_called_once_with('123\n')

    def test_mismatching_username_needs_local_confirmation(self):
        update = [{'update_id': 13, 'message': {'chat': {'type': 'private'},
                  'from': {'id': 456, 'username': 'different'}, 'text': '/start'}}]
        with patch('app.main.Path.read_text', side_effect=['token', 'EXAMPLE_USER']), \
             patch.object(Bot, 'api', side_effect=[{'username': 'testbot'}, update]), \
             patch('builtins.input', return_value='456'), \
             patch('app.main.Path.write_text'), patch('app.main.Path.chmod'), \
             patch('app.main.journal'):
            bot = Bot({'timezone': 'Europe/Moscow'})
        self.assertEqual(bot.owner, 456)

    def test_nested_caption_and_date(self):
        parser = PageParser()
        parser.feed(page(3, '2026-11-16T12:00:00+00:00'))
        self.assertEqual(parser.posts[0]['text'], 'Текст\nСтрока & ещё')
        self.assertEqual(parser.posts[0]['date'], '2026-11-16T12:00:00+00:00')

    def test_pagination_and_half_open_range(self):
        start = datetime(2026, 11, 15, tzinfo=timezone.utc)
        end = datetime(2026, 11, 16, tzinfo=timezone.utc)
        pages = [page(4, '2026-11-16T00:00:00+00:00')+page(3, '2026-11-15T12:00:00+00:00'),
                 page(2, '2026-11-14T12:00:00+00:00')]
        with patch('app.source.fetch', side_effect=pages):
            posts, complete, _, _ = collect('test', start, end,
                {'max_pages_per_channel': 5, 'request_delay': 0}, lambda p: None)
        self.assertTrue(complete)
        self.assertEqual([p['key'] for p in posts], ['test/3'])

    def test_repeated_page_is_partial(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        with patch('app.source.fetch', return_value=page(3, '2026-11-15T12:00:00+00:00')):
            _, complete, _, _ = collect('test', start, datetime(2026, 11, 16, tzinfo=timezone.utc),
                {'max_pages_per_channel': 5, 'request_delay': 0}, lambda p: None)
        self.assertFalse(complete)

    def test_dst_calendar_days(self):
        start, end = custom_period('2026-03-29 2026-03-29', ZoneInfo('Europe/Berlin'),
                                   datetime(2026, 11, 16, tzinfo=timezone.utc))
        self.assertEqual((end-start).total_seconds(), 23*3600)

    def test_invented_citation_rejected(self):
        with self.assertRaises(RuntimeError):
            validate({'events': [{'title': 'x', 'details': ['x'], 'source_keys': ['fake/1']}]}, {'test/1'})

    def test_split_no_loss(self):
        text = 'А'*8000
        parts = list(split_text(text))
        self.assertTrue(all(len(p) <= 3500 for p in parts))
        self.assertEqual(''.join(parts), text)


if __name__ == '__main__':
    unittest.main()
