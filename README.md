# OSK-Raspberry-Trixie

Экранная клавиатура для Raspberry Pi OS Trixie (labwc, Wayland): wvkbd с широкой раскладкой EN/RU,
значок раскладки `osk-tray` и история буфера обмена `clipd` (синхронизация через Syncthing).

| Файл | Назначение |
|---|---|
| `osk` | управление клавиатурой: `osk toggle`, `osk restart`, `osk build`, `osk layer en|ru`, `osk slot` |
| `osk-tray` | значок раскладки: ЛКМ - язык (значок плавающий, перетаскивается), двойной клик - показать/скрыть, СКМ - история буфера, ПКМ - меню (над клавиатурой, если она на экране) |
| `clipd` | история буфера обмена: окно можно двигать, масштабировать, закрывать; двойной клик по записи вставляет её в прежнее окно |
| `osk.toml` | общий конфиг (-> `~/.config/osk/osk.toml`) |
| `patch_keyboard.py`, `mkwide.py`, `wide.toml` | сборка wvkbd-wide (upstream commit bbecd99) |
| `ICONs/tray/` | картинки значка |

Установка: скопировать `osk osk-tray clipd` в `~/.local/bin/`, `osk.toml` в `~/.config/osk/`, `ICONs` рядом с `osk-tray`
на Pi командой `osk build`.
Значок плавающий: перетащите его левой кнопкой, позиция запоминается. `osk-tray hide|show|toggle` - управление клавиатурой из терминала.
