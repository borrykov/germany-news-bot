#!/bin/zsh
cd "${0:A:h}"
python3 -m app.main
echo "Нажми Enter, чтобы закрыть окно."
read
