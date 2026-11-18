"""Generate a per-user LaunchAgent without personal paths or embedded secrets."""
import os
import plistlib
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
if not (root / '.env' / 'bot_token').exists():
    raise SystemExit('Сначала запусти Setup.command и настрой собственного бота.')
(root / 'data').mkdir(exist_ok=True, mode=0o700)
label = 'local.germany-news-bot-clean-copy'
# Prefer Homebrew Codex, but discover the installation rather than hardcode an account.
codex = shutil.which('codex')
if not codex:
    raise SystemExit('Установи Codex CLI, затем выполни codex login через ChatGPT.')
path = str(Path(codex).parent) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin')
config = {
    'Label': label,
    'ProgramArguments': ['/usr/bin/caffeinate', '-i', sys.executable, '-m', 'app.main'],
    'WorkingDirectory': str(root),
    'EnvironmentVariables': {'PATH': path, 'HOME': str(Path.home()), 'PYTHONUNBUFFERED': '1'},
    'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30,
    'StandardOutPath': str(root / 'data' / 'service.log'),
    'StandardErrorPath': str(root / 'data' / 'service-error.log'),
    'ProcessType': 'Background', 'ExitTimeOut': 30,
}
folder = Path.home() / 'Library' / 'LaunchAgents'
folder.mkdir(parents=True, exist_ok=True)
plist = folder / (label + '.plist')
plist.write_bytes(plistlib.dumps(config))
plist.chmod(0o600)
print('Создан файл автозапуска копии:', plist)
