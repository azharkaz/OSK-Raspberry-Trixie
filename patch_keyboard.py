#!/usr/bin/env python3
"""patch_keyboard.py - наши правки wvkbd (keyboard.c и keyboard.h), безопасно запускать повторно.

Запуск из каталога wvkbd:  python3 patch_keyboard.py
Перед первым изменением сохраняет keyboard.c.orig и keyboard.h.orig.
Каждая правка применяется только если все её шаблоны найдены, иначе файлы не трогаются.

Правки:
  1. caps       метки кириллических букв тоже становятся заглавными при Caps
                (в оригинале проверка strlen(label) == 1 подходит только латинице).
  2. mod-lock   залипание Shift/Ctrl/Alt/Win/AltGr: двойное быстрое нажатие (< 400 мс)
                фиксирует модификатор, простое нажатие снимает. При залипшем Shift
                пробел остаётся пробелом (а не Tab).
  3. hold       у клавиши можно задать действие на удержание (>= 450 мс):
                hold_code/hold_mod (комбинация) или hold_cmd (команда оболочки).
                Такая клавиша срабатывает при отпускании: короткое нажатие - обычное
                действие, долгое - действие на удержание.
  4. layout-sync синхронизация раскладки экранной клавиатуры с системной (main.c и keyboard.c):
                - сигнал SIGRTMIN+N выбирает N-й слой из списка слоёв (-l wideen,wideru):
                  SIGRTMIN+1 = первый (EN), SIGRTMIN+2 = второй (RU); слой меняется и при
                  скрытой клавиатуре, управляющие сигналы блокируются с самого старта;
                - нажатие кнопки раскладки на экране запускает команду из $OSK_LAYOUT_CMD,
                  ей передаётся имя раскладки (например wideru): по ней система меняет язык.
"""
import re
import shutil
import sys
from pathlib import Path

KC, KH, KM = Path("keyboard.c"), Path("keyboard.h"), Path("main.c")
M_CAPS = "/* osk-patch: caps_affects */"
M_LOCK = "/* osk-patch: mod-lock+hold */"
M_SYNC = "/* osk-patch: layout-sync */"

CAPS_HELPER = M_CAPS + r'''
static int caps_affects(const char *label) {
    unsigned char c = (unsigned char)label[0];
    if (c == 0)
        return 0;
    if (c < 0x80)                              /* одна латинская буква */
        return label[1] == 0 && isalpha(c);
    /* одна кириллическая буква (U+0400..U+04FF): байты D0/D1 + ещё один */
    return (c == 0xD0 || c == 0xD1) && label[1] != 0 && label[2] == 0;
}
'''

LOCK_PROTO = M_LOCK + r'''
#include <signal.h>
#include <unistd.h>
#define OSK_HOLD_MS 450
#define OSK_DBLTAP_MS 400
static void osk_fire_hold(struct kbd *kb, uint32_t time);
static int osk_mod_double_tap(struct kbd *kb, struct key *k, uint32_t time);
'''

LOCK_BODIES = r'''/* ---- osk-patch: залипание модификаторов и действия на удержание ---- */
static void osk_run(const char *cmd)
{
    static int inited;
    if (!inited) {
        signal(SIGCHLD, SIG_IGN);
        inited = 1;
    }
    if (fork() == 0) {
        setsid();
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
}

static void osk_send_combo(struct kbd *kb, uint32_t time, uint32_t mod, uint32_t code)
{
    uint32_t add = mod & ~kb->mods;      /* модификаторы, которых ещё нет в нажатых */
    if (add) {
        zwp_virtual_keyboard_v1_key_mods(kb->vkbd, time, add, WL_KEYBOARD_KEY_STATE_PRESSED);
        zwp_virtual_keyboard_v1_modifiers(kb->vkbd, kb->mods | mod, 0, 0, 0);
    }
    zwp_virtual_keyboard_v1_key(kb->vkbd, time + 1, code, WL_KEYBOARD_KEY_STATE_PRESSED);
    zwp_virtual_keyboard_v1_key(kb->vkbd, time + 2, code, WL_KEYBOARD_KEY_STATE_RELEASED);
    if (add) {
        zwp_virtual_keyboard_v1_key_mods(kb->vkbd, time + 3, add, WL_KEYBOARD_KEY_STATE_RELEASED);
        zwp_virtual_keyboard_v1_modifiers(kb->vkbd, kb->mods, 0, 0, 0);
    }
}

static void osk_fire_hold(struct kbd *kb, uint32_t time)
{
    struct key *k = kb->hold_pending;
    uint32_t un;
    int held = (uint32_t)(time - kb->press_time) >= OSK_HOLD_MS;
    kb->hold_pending = NULL;
    kb->last_press = NULL;
    if (held && k->hold_cmd)
        osk_run(k->hold_cmd);
    else if (held && k->hold_code)
        osk_send_combo(kb, time, k->hold_mod, k->hold_code);
    else
        osk_send_combo(kb, time, k->code_mod, k->code);
    /* как у обычных клавиш: одноразовые модификаторы снимаются после нажатия */
    un = kb->mods & ~kb->locked_mods & (Shift | Ctrl | Alt | Super | AltGr);
    if (un) {
        zwp_virtual_keyboard_v1_key_mods(kb->vkbd, time + 4, un, WL_KEYBOARD_KEY_STATE_RELEASED);
        kb->mods &= ~un;
        zwp_virtual_keyboard_v1_modifiers(kb->vkbd, kb->mods, 0, 0, 0);
    }
    kbd_draw_key(kb, k, Unpress);
    if (un & Shift)
        kbd_draw_layout(kb);
}

static int osk_mod_double_tap(struct kbd *kb, struct key *k, uint32_t time)
{
    uint32_t m = k->code;
    int consumed = 0;
    kb->locked_mods &= kb->mods;
    if (m == Shift || m == Ctrl || m == Alt || m == Super || m == AltGr) {
        if (kb->locked_mods & m) {
            kb->locked_mods &= ~m;                 /* простое нажатие снимает залипание */
        } else if ((kb->mods & m) && kb->last_mod_code == m &&
                   (uint32_t)(time - kb->last_mod_time) < OSK_DBLTAP_MS) {
            kb->locked_mods |= m;                  /* двойное быстрое нажатие: залипание */
            consumed = 1;
        }
    }
    kb->last_mod_code = m;
    kb->last_mod_time = time;
    return consumed;
}

'''

HOLD_HOOK = r'''        if (k->hold_code || k->hold_cmd) {     /* osk-patch: срабатывает при отпускании */
            kb->last_swipe = kb->last_press = k;
            kb->last_press_offset = time - monotime();
            kb->hold_pending = k;
            kb->press_time = time;
            kbd_draw_key(kb, k, Press);
            break;
        }
'''

CAPS_RE = re.compile(r"strlen\(\s*k->label\s*\)\s*==\s*1\s*&&\s*isalpha\(\s*k->label\[0\]\s*\)")


def sub1(text, pattern, repl, what, count=1, hint=None):
    new, n = re.subn(pattern, repl, text, flags=re.M)
    if n != count:
        msg = f"«{what}»: найдено {n} совпадений, ожидалось {count}"
        if hint:
            near = [ln for ln in text.splitlines() if hint in ln][:3]
            msg += f"\nСтроки с «{hint}» в файле (repr): {near}"
        raise ValueError(msg)
    return new


def patch_caps(c):
    if M_CAPS in c:
        return c, False
    n = len(CAPS_RE.findall(c))
    if n < 1:
        raise ValueError("«caps»: не нашёл проверку isalpha(label)")
    c = CAPS_RE.sub("caps_affects(k->label)", c)
    inc = list(re.finditer(r"^#include[^\n]*\n", c, re.M))
    if not inc:
        raise ValueError("«caps»: нет #include в keyboard.c")
    pos = inc[-1].end()
    return c[:pos] + "\n" + CAPS_HELPER + c[pos:], True


def patch_lock(c, h):
    if M_LOCK in c:
        return c, h, False
    # --- keyboard.h ---
    h = sub1(h, r"^([ \t]*)(bool reset_mod;[^\n]*\n)",
             lambda m: m.group(1) + m.group(2) +
             f"{m.group(1)}uint32_t hold_code;      /* osk: клавиша для удержания (0 - нет) */\n"
             f"{m.group(1)}uint32_t hold_mod;       /* osk: модификатор для удержания */\n"
             f"{m.group(1)}const char *hold_cmd;    /* osk: команда оболочки на удержание */\n",
             "struct key", hint="reset_mod")
    h = sub1(h, r"(struct kbd \{\n)([ \t]*)(bool debug;\n)",
             lambda m: m.group(1) + m.group(2) + m.group(3) +
             f"{m.group(2)}uint32_t locked_mods;    /* osk: залипшие модификаторы */\n"
             f"{m.group(2)}uint32_t last_mod_code, last_mod_time, press_time;\n"
             f"{m.group(2)}struct key *hold_pending;\n",
             "struct kbd", hint="struct kbd")
    # --- keyboard.c ---
    inc = list(re.finditer(r"^#include[^\n]*\n", c, re.M))
    if not inc:
        raise ValueError("«mod-lock»: нет #include в keyboard.c")
    pos = inc[-1].end()
    c = c[:pos] + "\n" + LOCK_PROTO + c[pos:]
    c = sub1(c, r"(unlatch_\w+ = \(kb->mods & (Shift|Ctrl|AltGr|Alt|Super)\) == \2)(;)",
             lambda m: f"{m.group(1)} && !(kb->locked_mods & {m.group(2)}){m.group(3)}",
             "unlatch", count=5, hint="unlatch_shift =")
    c = sub1(c, r"(void\nkbd_unpress_key\(struct kbd \*kb, uint32_t time\)\n\{\n)",
             lambda m: m.group(1) + "    if (kb->hold_pending)\n        osk_fire_hold(kb, time);\n"
                                    "    kb->locked_mods &= kb->mods;\n",
             "kbd_unpress_key", hint="kbd_unpress_key")
    c = sub1(c, r"(\nvoid\nkbd_press_key\(struct kbd \*kb, struct key \*k, uint32_t time\)\n\{)",
             lambda m: "\n" + LOCK_BODIES + m.group(1), "kbd_press_key", hint="kbd_press_key")
    c = sub1(c, r"([ \t]*case Code:\n)([ \t]*if \(k->code_mod\) \{\n)",
             lambda m: m.group(1) + HOLD_HOOK + m.group(2), "case Code", hint="case Code")
    c = sub1(c, r"([ \t]*case Mod:\n)([ \t]*kb->mods \^= k->code;\n)",
             lambda m: m.group(1) + "        if (osk_mod_double_tap(kb, k, time))\n            break;\n" + m.group(2),
             "case Mod", hint="case Mod")
    c = sub1(c, r"(\(kb->shift_space_is_tab\) && \(k->code == KEY_SPACE\) && \(kb->mods & Shift\))(\) \{)",
             lambda m: m.group(1) + " && !(kb->locked_mods & Shift)" + m.group(2),
             "shift+space", hint="shift_space_is_tab")
    return c, h, True


SYNC_KC_FUNC = M_SYNC + r"""
extern int osk_pref_layer;   /* main.c: слой, который надо вернуть при следующем показе клавиатуры */

/* Нажатие кнопки раскладки на экране: сообщаем системе командой из $OSK_LAYOUT_CMD <имя раскладки> */
static void osk_layout_changed(struct kbd *kb)
{
    const char *cmd = getenv("OSK_LAYOUT_CMD");
    char buf[512];
    enum layout_id *lst = kb->landscape ? kb->landscape_layers : kb->layers;
    int i;
    for (i = 0; i < MAX_LAYERS && lst[i] != NumLayouts; i++) {
        if (&kb->layouts[lst[i]] == kb->layout) {
            osk_pref_layer = i;
            break;
        }
    }
    if (!cmd || !*cmd || !kb->layout || !kb->layout->name)
        return;
    snprintf(buf, sizeof buf, "%s '%s'", cmd, kb->layout->name);
    osk_run(buf);
}

"""

SYNC_MAIN_EARLY = M_SYNC + r"""
    /* osk-patch: управляющие сигналы блокируем сразу при старте: пока не создан signalfd,
       «ранний» сигнал иначе завершил бы процесс */
    {
        sigset_t osk_early;
        int osk_n;
        sigemptyset(&osk_early);
        sigaddset(&osk_early, SIGUSR1);
        sigaddset(&osk_early, SIGUSR2);
        for (osk_n = 0; osk_n <= OSK_MAX_LAYER_SIGS; osk_n++)
            sigaddset(&osk_early, SIGRTMIN + osk_n);
        sigprocmask(SIG_BLOCK, &osk_early, NULL);
        signal(SIGCHLD, SIG_IGN);
    }
"""

SYNC_MAIN_PREF = M_SYNC + r"""
/* Слой, выбранный пользователем или системой. Штатный wvkbd при каждом показе клавиатуры сбрасывает слой на
   первый, из-за этого после скрытия и показа раскладка расходилась с системной. */
int osk_pref_layer = 0;

/* Метка для osk и osk-tray: по ней они узнают, что этот wvkbd понимает сигналы смены слоя
   (старый wvkbd от такого сигнала завершается, поэтому без метки сигнал не посылают). */
const char osk_layer_sync_marker[] = "OSK_LAYER_SYNC_V1";
"""

REDIM_RE = re.compile(
    r"    enum layout_id layer;\n"
    r"    if \(keyboard\.landscape\) \{\n"
    r"        layer = keyboard\.landscape_layers\[0\];\n"
    r"        height = landscape_height;\n"
    r"    \} else \{\n"
    r"        layer = keyboard\.layers\[0\];\n"
    r"        height = normal_height;\n"
    r"    \}\n")

REDIM_NEW = r"""    enum layout_id layer;
    /* osk-patch: layout-sync: при показе возвращаем выбранный слой (а не всегда первый) */
    struct layout *osk_old = keyboard.layout;
    {
        enum layout_id *osk_lst = keyboard.landscape ? keyboard.landscape_layers : keyboard.layers;
        int osk_cnt = 0;
        while (osk_cnt < MAX_LAYERS && osk_lst[osk_cnt] != NumLayouts)
            osk_cnt++;
        layer = osk_lst[(osk_pref_layer >= 0 && osk_pref_layer < osk_cnt) ? osk_pref_layer : 0];
    }
    height = keyboard.landscape ? landscape_height : normal_height;
"""

REDIM_TAIL_RE = re.compile(
    r"(    keyboard\.prevlayout = keyboard\.layout;\n"
    r"    keyboard\.last_abc_layout = keyboard\.layout;\n"
    r"    keyboard\.last_abc_index = 0;\n)\}")

REDIM_TAIL_NEW = r"""    /* osk-patch: layout-sync: подпись клавиш и раскладка ввода должны совпадать */
    if (osk_old && osk_old != keyboard.layout && keyboard.vkbd &&
        strcmp(osk_old->keymap_name, keyboard.layout->keymap_name) != 0)
        create_and_upload_keymap(&keyboard, keyboard.layout->keymap_name, 0);
}"""

SYNC_MAIN_FUNC = M_SYNC + r"""
#define OSK_MAX_LAYER_SIGS 8

/* Выбор слоя извне: сигнал SIGRTMIN+N выбирает N-й слой списка слоёв (нумерация с 1).
   Повторный выбор того же слоя ничего не делает, поэтому системе можно слать состояние сколько угодно раз. */
static void
osk_set_layer(int n)
{
    enum layout_id *list =
        keyboard.landscape ? keyboard.landscape_layers : keyboard.layers;
    int count = 0;
    struct layout *l, *prev;

    while (count < MAX_LAYERS && list[count] != NumLayouts)
        count++;
    if (n < 0 || n >= count)
        return;
    osk_pref_layer = n;
    l = &keyboard.layouts[list[n]];
    prev = keyboard.layout;
    if (prev == l)
        return;
    if (hidden || !layer_surface) {
        /* клавиатура скрыта: поверхности нет, рисовать нельзя - меняем только состояние и раскладку ввода */
        keyboard.prevlayout = prev;
        keyboard.layout = l;
        keyboard.layer_index = kbd_get_layer_index(&keyboard, l);
        if (!prev || strcmp(prev->keymap_name, l->keymap_name) != 0)
            create_and_upload_keymap(&keyboard, l->keymap_name, 0);
    } else {
        kbd_switch_layout(&keyboard, l, kbd_get_layer_index(&keyboard, l));
    }
    keyboard.compose = 0;
    keyboard.last_abc_index = 0;
    keyboard.last_abc_layout = &keyboard.layouts[list[0]];
}

"""

SYNC_MAIN_SIGNALS = M_SYNC + r"""
    for (int osk_n = 1; osk_n <= OSK_MAX_LAYER_SIGS; osk_n++)
        sigaddset(&signal_mask, SIGRTMIN + osk_n);
"""

SYNC_MAIN_HANDLER = r"""            else if ((int)si.ssi_signo > SIGRTMIN &&
                     (int)si.ssi_signo <= SIGRTMIN + OSK_MAX_LAYER_SIGS)
                osk_set_layer((int)si.ssi_signo - SIGRTMIN - 1); /* osk-patch: layout-sync */
"""

LAYOUT_CASE_RE = re.compile(
    r"(    case Layout:\n        // switch to the layout determined by the key\n(?:.*\n)*?)(        break;\n    case Compose:)")


def patch_sync(c, m):
    """keyboard.c + main.c: синхронизация слоя с системной раскладкой. Требует патч mod-lock+hold (osk_run)."""
    if M_SYNC in c and M_SYNC in m:
        return c, m, False
    if M_LOCK not in c:
        raise ValueError("«layout-sync»: сначала должен примениться патч mod-lock+hold")
    # --- keyboard.c ---
    c = sub1(c, r"(\nvoid\nkbd_press_key\(struct kbd \*kb, struct key \*k, uint32_t time\)\n\{)",
             lambda mm: "\n" + SYNC_KC_FUNC + mm.group(1), "kbd_press_key (layout-sync)", hint="kbd_press_key")
    if not LAYOUT_CASE_RE.search(c):
        raise ValueError("«layout-sync»: не нашёл обработчик case Layout в keyboard.c")
    c = LAYOUT_CASE_RE.sub(lambda mm: mm.group(1) +
                           "        osk_layout_changed(kb);        /* osk-patch: layout-sync */\n" + mm.group(2), c, count=1)
    # дочерние процессы не должны наследовать заблокированные сигналы
    c = sub1(c, r'(        setsid\(\);\n)(        execl\("/bin/sh")',
             lambda mm: mm.group(1) + "        { sigset_t osk_all; sigfillset(&osk_all); sigprocmask(SIG_UNBLOCK, &osk_all, NULL); }\n" + mm.group(2),
             "osk_run: сигналы потомка", hint="setsid")
    # --- main.c ---
    m = sub1(m, r'(#include "keyboard.h"\n)', lambda mm: mm.group(1) + "#include <signal.h>\n", "include signal.h", hint="keyboard.h")
    m = sub1(m, r"(\nvoid\nredimension_keyboard\(\)\n\{)", lambda mm: "\n" + SYNC_MAIN_PREF + mm.group(1),
             "redimension_keyboard (pref)", hint="redimension_keyboard")
    if not REDIM_RE.search(m) or not REDIM_TAIL_RE.search(m):
        raise ValueError("«layout-sync»: не нашёл redimension_keyboard в main.c")
    m = REDIM_RE.sub(lambda mm: REDIM_NEW, m, count=1)
    m = REDIM_TAIL_RE.sub(lambda mm: mm.group(1) + REDIM_TAIL_NEW, m, count=1)
    m = sub1(m, r"(\nvoid\ntoggle_visibility\(\)\n\{)", lambda mm: "\n" + SYNC_MAIN_FUNC + mm.group(1),
             "toggle_visibility", hint="toggle_visibility")
    m = sub1(m, r"(\nmain\(int argc, char \*\*argv\)\n\{\n)", lambda mm: mm.group(1) + SYNC_MAIN_EARLY, "main()", hint="main(")
    m = sub1(m, r"([ \t]*sigaddset\(&signal_mask, SIGRTMIN\);\n)", lambda mm: mm.group(1) + SYNC_MAIN_SIGNALS,
             "signal_mask", hint="signal_mask")
    m = sub1(m, r"([ \t]*else if \(si\.ssi_signo == SIGPIPE\))", lambda mm: SYNC_MAIN_HANDLER + mm.group(1),
             "SIGPIPE handler", hint="SIGPIPE")
    return c, m, True


def main():
    if not KC.exists() or not KH.exists() or not KM.exists():
        sys.exit("patch_keyboard: нет keyboard.c/keyboard.h/main.c, запускай из каталога wvkbd")
    c, h, m = KC.read_text(encoding="utf-8"), KH.read_text(encoding="utf-8"), KM.read_text(encoding="utf-8")
    try:
        c, ch_caps = patch_caps(c)
        c, h, ch_lock = patch_lock(c, h)
        c, m, ch_sync = patch_sync(c, m)
    except ValueError as e:
        sys.exit(f"patch_keyboard: {e}\nФайлы не изменены. Версия wvkbd отличается - пришли соответствующий кусок кода.")
    if not (ch_caps or ch_lock or ch_sync):
        print("patch_keyboard: все правки уже применены")
        return
    for f in (KC, KH, KM):
        if not Path(str(f) + ".orig").exists():
            shutil.copyfile(f, str(f) + ".orig")
    KC.write_text(c, encoding="utf-8")
    KH.write_text(h, encoding="utf-8")
    KM.write_text(m, encoding="utf-8")
    done = [n for n, ok in (("caps", ch_caps), ("mod-lock+hold", ch_lock), ("layout-sync", ch_sync)) if ok]
    print("patch_keyboard: применено: " + ", ".join(done))


if __name__ == "__main__":
    main()
