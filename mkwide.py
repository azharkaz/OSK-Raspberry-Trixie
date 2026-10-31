#!/usr/bin/env python3
"""mkwide.py - генератор раскладки wvkbd из TOML-описания.

Запуск из каталога с исходниками (~/wvkbd):
    python3 mkwide.py [wide.toml]
    make LAYOUT=wide wvkbd-wide

Из layout/keymap/config.mobintl.h делает layout/keymap/config.<name>.h,
добавляя раскладки из TOML. Исходные файлы mobintl не меняются.

Ряд в TOML - строка из ключей через пробел, у каждого можно задать ширину
после двоеточия (по умолчанию 1.0):
    q            буква (код KEY_Q, на shift Q)
    Esc F1 Enter именованные клавиши (встроенные и из таблицы [keys])
    _:0.5        пустое место
    >ru:1.25     кнопка переключения на раскладку ru
    й=Q          своя метка, код клавиши после "=" (KEY_ добавляется сам)
    (в [keys] у клавиши можно задать mod = "Ctrl" - комбинация, напр. Ctrl+C;
     hold_code/hold_mod/hold_cmd - что делать при удержании клавиши, tap_cmd - команда на короткое нажатие)
    FBtn:1       круглая кнопка плавающего режима (клик - свернуть/развернуть, перетаскивание - двигать)
    4/;=4        метка/метка на shift = код
"""
import re
import shutil
import sys
import tomllib
from pathlib import Path

SRC = "mobintl"
SPEC = Path(sys.argv[1] if len(sys.argv) > 1 else "wide.toml")


def _k(label, shift=None, type="Code", code=None, scheme=0):
    return {"l": label, "s": label if shift is None else shift,
            "type": type, "code": code, "scheme": scheme}


BUILTIN = {
    "Esc": _k("Esc", code="KEY_ESC", scheme=1),
    "Tab": _k("Tab", code="KEY_TAB", scheme=1),
    "Caps": _k("Caps", type="Mod", code="CapsLock", scheme=1),
    "Shift": _k("Shift", type="Mod", code="Shift", scheme=1),
    "Ctrl": _k("Ctrl", type="Mod", code="Ctrl", scheme=1),
    "Alt": _k("Alt", type="Mod", code="Alt", scheme=1),
    "AltGr": _k("AltGr", type="Mod", code="AltGr", scheme=1),
    "Win": _k("Win", type="Mod", code="Super", scheme=1),
    "Menu": _k("Menu", code="KEY_MENU", scheme=1),
    "Enter": _k("Enter", code="KEY_ENTER", scheme=1),
    "Bksp": _k("\u232b", code="KEY_BACKSPACE", scheme=1),
    "Space": _k("", "Tab", code="KEY_SPACE"),
    "PrtSc": _k("PrtSc", code="KEY_SYSRQ", scheme=1),
    "ScrLk": _k("ScrLk", code="KEY_SCROLLLOCK", scheme=1),
    "Pause": _k("Pause", code="KEY_PAUSE", scheme=1),
    "Ins": _k("Ins", code="KEY_INSERT", scheme=1),
    "Del": _k("Del", code="KEY_DELETE", scheme=1),
    "Home": _k("Home", code="KEY_HOME", scheme=1),
    "End": _k("End", code="KEY_END", scheme=1),
    "PgUp": _k("PgUp", code="KEY_PAGEUP", scheme=1),
    "PgDn": _k("PgDn", code="KEY_PAGEDOWN", scheme=1),
    "Up": _k("\u2191", code="KEY_UP", scheme=1),
    "Down": _k("\u2193", code="KEY_DOWN", scheme=1),
    "Left": _k("\u2190", code="KEY_LEFT", scheme=1),
    "Right": _k("\u2192", code="KEY_RIGHT", scheme=1),
    "grave": _k("`", "~", code="KEY_GRAVE"),
    "minus": _k("-", "_", code="KEY_MINUS"),
    "equal": _k("=", "+", code="KEY_EQUAL"),
    "lbrace": _k("[", "{", code="KEY_LEFTBRACE"),
    "rbrace": _k("]", "}", code="KEY_RIGHTBRACE"),
    "bslash": _k("\\", "|", code="KEY_BACKSLASH"),
    "semi": _k(";", ":", code="KEY_SEMICOLON"),
    "apos": _k("'", '"', code="KEY_APOSTROPHE"),
    "comma": _k(",", "<", code="KEY_COMMA"),
    "dot": _k(".", ">", code="KEY_DOT"),
    "slash": _k("/", "?", code="KEY_SLASH"),
}
for i in range(1, 13):
    BUILTIN[f"F{i}"] = _k(f"F{i}", code=f"KEY_F{i}", scheme=1)
for d, s in zip("1234567890", "!@#$%^&*()"):
    BUILTIN[d] = _k(d, s, code=f"KEY_{d}")


def die(msg):
    sys.exit(f"mkwide: {msg}")


def cstr(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def enum_name(name, lid):
    return name.capitalize() + lid.capitalize()


def parse_token(tok, keys):
    m = re.match(r"^(.*?)(?::(\d+(?:\.\d+)?))?$", tok)
    body, w = m.group(1), float(m.group(2) or 1.0)
    if body == "_":
        return {"type": "Pad", "w": w, "l": "", "s": "", "scheme": 1}
    if body == "FBtn":                            # круглая кнопка плавающего режима (нужен патч float)
        return {"type": "FloatBtn", "w": w, "l": "", "s": "", "scheme": 1}
    if body.startswith(">"):
        return {"type": "Layout", "w": w, "target": body[1:], "scheme": 0}
    if "=" in body:
        lab, code = body.rsplit("=", 1)
        if "/" in lab:
            l, s = lab.split("/", 1)
        else:
            l, s = lab, (lab.upper() if lab.isalpha() else lab)
        return {"type": "Code", "w": w, "l": l, "s": s, "code": "KEY_" + code.upper(), "scheme": 0}
    d = keys.get(body) or BUILTIN.get(body)
    if d is None and len(body) == 1 and body.isascii() and body.isalpha():
        d = _k(body, body.upper(), code="KEY_" + body.upper())
    if d is None:
        die(f"неизвестная клавиша «{tok}»")
    d = dict(d)
    d.setdefault("type", "Code")
    d.setdefault("scheme", 0)
    d["s"] = d.get("s", d["l"])
    d["w"] = w
    return d


def emit_key(k, name, spec_layouts):
    sch = f", .scheme = {k['scheme']}" if k.get("scheme") else ""
    w = repr(float(k["w"]))
    t = k["type"]
    if t == "Pad":
        return f'  {{"", "", {w}, Pad{sch}}},'
    if t == "FloatBtn":
        return f'  {{"", "", {w}, FloatBtn, 0}},'
    if t == "Layout":
        tgt = k["target"]
        if tgt not in spec_layouts:
            die(f"кнопка >{tgt}: такой раскладки нет в TOML")
        lab = cstr(spec_layouts[tgt].get("label", tgt.upper()))
        return f"  {{{lab}, {lab}, {w}, Layout, 0, &layouts[{enum_name(name, tgt)}]{sch}}},"
    mod = f", 0, {k['mod']}" if k.get("mod") and t == "Code" else ""    # code_mod: модификатор на время нажатия
    hold = ""                                    # действия на удержание (нужен patch_keyboard.py)
    if t == "Code":
        if k.get("hold_code"):
            hold += f", .hold_code = {k['hold_code']}"
        if k.get("hold_mod"):
            hold += f", .hold_mod = {k['hold_mod']}"
        if k.get("hold_cmd"):
            hold += f", .hold_cmd = {cstr(k['hold_cmd'])}"
        if k.get("tap_cmd"):                       # команда на короткое нажатие (нужен патч tap-cmd)
            hold += f", .tap_cmd = {cstr(k['tap_cmd'])}"
    return f"  {{{cstr(k['l'])}, {cstr(k['s'])}, {w}, {t}, {k['code']}{mod}{sch}{hold}}},"


def build_layout(name, lid, lay, keys, spec_layouts):
    lines = [f"static struct key keys_{name}_{lid}[] = {{"]
    sums = []
    for row in lay["rows"]:
        total = 0.0
        for tok in row.split():
            k = parse_token(tok, keys)
            total += k["w"]
            lines.append(emit_key(k, name, spec_layouts))
        sums.append(total)
        lines.append('  {"", "", 0.0, EndRow},\n')
    lines[-1] = '  /* end of layout */'
    lines.append('  {"", "", 0.0, Last},')
    lines.append("};\n")
    if len({round(s, 3) for s in sums}) > 1:
        print(f"mkwide: внимание, {lid}: суммы ширин рядов разные {sums} (ряды выровняются по ширине экрана)")
    return "\n".join(lines)


def patch(text, pattern, repl, what, count=1, flags=0):
    new, n = re.subn(pattern, repl, text, flags=flags)
    if n != count:
        die(f"не удалось применить патч «{what}» (совпадений {n}, ожидалось {count}); "
            "пришли соответствующий кусок файла")
    return new


def main():
    with open(SPEC, "rb") as f:
        spec = tomllib.load(f)
    st = spec["settings"]
    name = st.get("name", "wide")
    keys = spec.get("keys", {})
    lays = spec["layout"]
    ids = list(lays)

    for fn in ("layout", "keymap", "config"):
        if not Path(f"{fn}.{SRC}.h").exists():
            die(f"нет файла {fn}.{SRC}.h - запускай из каталога wvkbd")

    # ---- layout ----
    text = Path(f"layout.{SRC}.h").read_text(encoding="utf-8")
    enums = [enum_name(name, i) for i in ids]
    # новые раскладки идут ДО Index: wvkbd считает Index последним элементом и не ищет его по имени
    text = patch(text, r"(\n[ \t]*)Index,([ \t]*\n[ \t]*NumLayouts,)",
                 lambda m: "".join(f"{m.group(1)}{e}," for e in enums) + f"{m.group(1)}Index,{m.group(2)}",
                 "enum layout_id")
    arrays = ", ".join(f"keys_{name}_{i}[]" for i in ids)
    text = patch(text, r"keys_dialer\[\], keys_index\[\];",
                 lambda m: f"keys_dialer[], keys_index[], {arrays};", "объявления массивов")
    entries = "".join(
        f'  [{enum_name(name, i)}] = {{keys_{name}_{i}, {cstr(lays[i]["keymap"])}, '
        f'{cstr(name + i)}, {"true" if lays[i].get("primary", True) else "false"}}},\n'
        for i in ids)
    text = patch(text, r"([ \t]*\[Index\] = \{keys_index[^\n]*\n)",
                 lambda m: entries + m.group(1), "массив layouts")
    text = patch(text, r"(#define KBD_PIXEL_HEIGHT )\d+",
                 lambda m: m.group(1) + str(st.get("height", 300)), "высота")
    text = patch(text, r"(#define KBD_PIXEL_LANDSCAPE_HEIGHT )\d+",
                 lambda m: m.group(1) + str(st.get("landscape_height", 300)), "высота (ландшафт)")
    if not text.endswith("\n"):
        text += "\n"
    text += "\n/* ---- generated by mkwide.py from " + SPEC.name + " ---- */\n"
    for i in ids:
        text += "\n" + build_layout(name, i, lays[i], keys, lays)

    # ---- config ----
    cfg = Path(f"config.{SRC}.h").read_text(encoding="utf-8")
    if "font" in st:
        cfg = patch(cfg, r'(#define DEFAULT_FONT )"[^"]*"',
                    lambda m: m.group(1) + cstr(st["font"]), "шрифт")

    def layers_body(order):
        items = [enum_name(name, i) for i in order]
        out = [f"  {items[0]}, // First layout is the default layout on startup"]
        out += [f"  {e}," for e in items[1:]]
        out.append("  NumLayouts // signals the last item, may not be omitted")
        return "\n".join(out)

    cfg = patch(cfg, r"(static\s+enum\s+layout_id\s+layers\[\]\s*=\s*\{)(.*?)(\n?\};)",
                lambda m: m.group(1) + "\n" + layers_body(st.get("layers", ids)) + m.group(3),
                "layers", flags=re.S)
    cfg, n = re.subn(r"(static\s+enum\s+layout_id\s+landscape_layers\[\]\s*=\s*\{)(.*?)(\n?\};)",
                     lambda m: m.group(1) + "\n" + layers_body(st.get("landscape_layers", ids)) + m.group(3),
                     cfg, flags=re.S)
    if n != 1:
        print("mkwide: в config нет landscape_layers - слои для ландшафта останутся штатными")

    Path(f"layout.{name}.h").write_text(text, encoding="utf-8")
    Path(f"config.{name}.h").write_text(cfg, encoding="utf-8")
    shutil.copyfile(f"keymap.{SRC}.h", f"keymap.{name}.h")
    print(f"mkwide: готово: layout.{name}.h, config.{name}.h, keymap.{name}.h")
    print(f"        дальше: make LAYOUT={name} wvkbd-{name}")


if __name__ == "__main__":
    main()
