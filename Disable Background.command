#!/bin/zsh
# Disable first, so the service also stays off after the next macOS login.
domain="gui/$(id -u)"
label="local.germany-news-bot-clean-copy"
launchctl disable "$domain/$label"
launchctl bootout "$domain/$label" 2>/dev/null
echo "Фоновый запуск отключён. Для включения используй Enable Background.command."
read "?Нажми Enter, чтобы закрыть окно."
