#!/bin/sh
# Сборка пояснительной записки.
#
# Три прогона pdflatex нужны из-за содержания и ссылок: первый собирает документ,
# bibtex формирует список источников, оставшиеся два проставляют номера страниц
# и ссылки на литературу.
#
#   sh build.sh
#
# Требуется MiKTeX или TeX Live с пакетом cm-super: без него подставляются
# растровые шрифты и документ выглядит заметно хуже при печати.

set -e
cd "$(dirname "$0")"

MAIN=cource/cource_report.tex
JOB=cource_report

pdflatex -interaction=nonstopmode "$MAIN" > /dev/null
bibtex "$JOB" > /dev/null || true
pdflatex -interaction=nonstopmode "$MAIN" > /dev/null
pdflatex -interaction=nonstopmode "$MAIN" > "$JOB.build.log" 2>&1

if grep -q '^!' "$JOB.build.log"; then
  echo "Сборка завершилась с ошибками:"
  grep -n '^!' -A3 "$JOB.build.log"
  exit 1
fi

rm -f "$JOB.build.log"
echo "Готово: $JOB.pdf"
