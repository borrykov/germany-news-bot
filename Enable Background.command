#!/bin/zsh
# Build a new LaunchAgent for this copy; never reuse the original owner's service.
cd "${0:A:h}"
python3 install_background.py || exit 1
domain="gui/$(id -u)"
label="local.germany-news-bot-clean-copy"
plist="$HOME/Library/LaunchAgents/$label.plist"
launchctl enable "$domain/$label"
if ! launchctl print "$domain/$label" >/dev/null 2>&1; then
  launchctl bootstrap "$domain" "$plist" || exit 1
fi
echo "Фоновая служба копии включена. Закрой ручной экземпляр этой копии, если он работает."
read "?Нажми Enter, чтобы закрыть окно."
